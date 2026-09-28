"""Drift-significance classifier tests — Bite 3 of the Defined/Discovered
manifest lifecycle.

Spec: internal/spec-apps-meta-2026-06-13.md §9.3 (drift is fluid — narrate,
don't gate), §9.4 (scanner job flips with definition_status).

Three layers:
  1. ``classify_drift_event`` — the deterministic major/minor rule + the one
     LLM-escalation case (injected ``llm_fn``).
  2. ``narrate_drift`` / ``count_unreviewed_drift`` — the append + idempotency
     + ``defined``-only gating + unreviewed-marker data source.
  3. Integration: the exact scanner Phase-6 sequence (apply_reconciliation →
     narrate_drift) over real manifest-shaped fixtures — the PROOF cases:
     new script → MAJOR on a defined app; new .md → silent absorb, no entry;
     removed cron → MAJOR; discovered app → fresh content, no log.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import drift_classifier as dc  # noqa: E402
from evolve_admin.applications.reconciliation import (  # noqa: E402
    apply_reconciliation,
)


# ════════════════════════════════════════════════════════════════════════════
# Layer 1 — deterministic classification
# ════════════════════════════════════════════════════════════════════════════

def _ev(kind, target_type, target, **extra):
    return {"kind": kind, "target_type": target_type, "target": target, **extra}


def test_executable_path_detection() -> None:
    assert dc.is_executable_path("scripts/run.py") is True
    assert dc.is_executable_path("hooks/notify.sh") is True
    assert dc.is_executable_path("lib/tool.js") is True
    # data / doc
    assert dc.is_executable_path("data/2026-06-24.md") is False
    assert dc.is_executable_path("state/config.json") is False
    assert dc.is_executable_path("notes.txt") is False
    # extension-less file under scripts/ or bin/ → executable by intent
    assert dc.is_executable_path("scripts/run") is True
    assert dc.is_executable_path("bin/launch") is True
    # extension-less elsewhere → conservative not-executable (don't spam)
    assert dc.is_executable_path("data/payload") is False


def test_add_remove_script_is_major() -> None:
    assert dc.classify_drift_event(_ev("add", "file", "scripts/new.py")).is_major
    assert dc.classify_drift_event(_ev("remove", "file", "scripts/old.sh")).is_major


def test_add_remove_doc_is_minor() -> None:
    assert not dc.classify_drift_event(_ev("add", "file", "data/log.md")).is_major
    assert not dc.classify_drift_event(_ev("remove", "file", "out/result.json")).is_major


def test_cron_and_action_always_major() -> None:
    for tt in ("cron", "action", "dependency"):
        for kind in ("add", "remove", "modify"):
            v = dc.classify_drift_event(_ev(kind, tt, "x"))
            assert v.is_major, f"{kind} {tt} should be major"
            assert v.classifier == dc.CLASSIFIER_DETERMINISTIC


def test_modify_doc_file_is_minor() -> None:
    assert not dc.classify_drift_event(_ev("modify", "file", "data/x.md")).is_major


def test_unknown_kind_is_minor() -> None:
    assert not dc.classify_drift_event(_ev("frobnicate", "file", "scripts/x.py")).is_major


# ════════════════════════════════════════════════════════════════════════════
# Layer 1b — LLM escalation (the one ambiguous case: script CONTENT modify)
# ════════════════════════════════════════════════════════════════════════════

def test_modify_script_without_llm_defaults_minor() -> None:
    """No injected judge → conservative minor (don't spam the log)."""
    v = dc.classify_drift_event(_ev("modify", "file", "scripts/foo.py"))
    assert not v.is_major
    assert v.classifier == dc.CLASSIFIER_DETERMINISTIC


def test_modify_script_llm_major() -> None:
    calls = []

    def judge(prompt: str) -> str:
        calls.append(prompt)
        return "MAJOR — adds a new side effect"

    v = dc.classify_drift_event(_ev("modify", "file", "scripts/foo.py"), llm_fn=judge)
    assert v.is_major
    assert v.classifier == dc.CLASSIFIER_LLM
    assert len(calls) == 1               # the model WAS consulted
    assert "scripts/foo.py" in calls[0]   # prompt carries the target


def test_modify_script_llm_minor() -> None:
    v = dc.classify_drift_event(
        _ev("modify", "file", "scripts/foo.py"), llm_fn=lambda p: "MINOR")
    assert not v.is_major
    assert v.classifier == dc.CLASSIFIER_LLM


def test_modify_script_llm_error_defaults_minor() -> None:
    def boom(prompt: str) -> str:
        raise RuntimeError("model unavailable")

    v = dc.classify_drift_event(_ev("modify", "file", "scripts/foo.py"), llm_fn=boom)
    assert not v.is_major                 # default-to-minor on error
    assert v.classifier == dc.CLASSIFIER_LLM


def test_modify_script_llm_ambiguous_reply_defaults_minor() -> None:
    """A reply that mentions both / neither verdict is low-confidence → minor."""
    for reply in ("could be major or minor", "", "  ", "unsure"):
        v = dc.classify_drift_event(
            _ev("modify", "file", "scripts/foo.py"), llm_fn=lambda p, r=reply: r)
        assert not v.is_major, f"reply {reply!r} should default minor"


def test_non_script_modify_never_calls_llm() -> None:
    """A cron/action/doc modify is settled deterministically — no model call."""
    calls = []
    judge = lambda p: (calls.append(p), "MAJOR")[1]
    dc.classify_drift_event(_ev("modify", "action", "a1"), llm_fn=judge)
    dc.classify_drift_event(_ev("modify", "file", "data/x.md"), llm_fn=judge)
    assert calls == []


# ════════════════════════════════════════════════════════════════════════════
# Layer 2 — narrate_drift gating, entry shape, idempotency, unreviewed count
# ════════════════════════════════════════════════════════════════════════════

def test_narrate_only_logs_major() -> None:
    m = {}
    events = [
        _ev("add", "file", "scripts/a.py"),    # major
        _ev("add", "file", "data/b.md"),        # minor → silent
        _ev("remove", "cron", "nightly"),       # major
    ]
    n = dc.narrate_drift(m, events, definition_status="defined")
    assert n == 2
    targets = {e["target"] for e in m["drift_log"]}
    assert targets == {"scripts/a.py", "nightly"}


def test_narrate_entry_shape() -> None:
    m = {}
    dc.narrate_drift(m, [_ev("remove", "file", "scripts/x.py")],
                     definition_status="defined", now="2026-06-24T00:00:00+00:00")
    e = m["drift_log"][0]
    assert e["ts"] == "2026-06-24T00:00:00+00:00"
    assert e["kind"] == "remove"
    assert e["target_type"] == "file"
    assert e["target"] == "scripts/x.py"
    assert e["significance"] == "major"
    assert e["reviewed"] is False
    assert e["source"] == dc.DRIFT_LOG_SOURCE
    assert e["classifier"] == dc.CLASSIFIER_DETERMINISTIC
    assert "scripts/x.py" in e["summary"]


def test_narrate_noop_for_discovered() -> None:
    """A ``discovered`` app is a churnable draft — fresh content, no narrative."""
    m = {}
    n = dc.narrate_drift(m, [_ev("remove", "file", "scripts/x.py")],
                         definition_status="discovered")
    assert n == 0
    assert m.get("drift_log", []) == []


def test_narrate_noop_for_absent_status() -> None:
    """Absent/empty definition_status reads as discovered (safe default)."""
    m = {}
    assert dc.narrate_drift(m, [_ev("remove", "cron", "x")], definition_status="") == 0


def test_narrate_idempotent_on_unreviewed_duplicate() -> None:
    """A staged (authored) delta recurs every scan; the unreviewed-dup guard
    keeps the log from accruing duplicates."""
    m = {}
    ev = [_ev("remove", "file", "scripts/x.py")]
    assert dc.narrate_drift(m, ev, definition_status="defined") == 1
    assert dc.narrate_drift(m, ev, definition_status="defined") == 0
    assert dc.narrate_drift(m, ev, definition_status="defined") == 0
    assert len(m["drift_log"]) == 1


def test_narrate_reviewed_recurrence_relogs() -> None:
    """Once the operator reviews an entry, a genuine recurrence re-narrates."""
    m = {}
    ev = [_ev("remove", "file", "scripts/x.py")]
    dc.narrate_drift(m, ev, definition_status="defined")
    m["drift_log"][0]["reviewed"] = True       # operator looked
    assert dc.narrate_drift(m, ev, definition_status="defined") == 1
    assert len(m["drift_log"]) == 2


def test_narrate_handles_corrupt_drift_log() -> None:
    """A non-list drift_log is replaced, not crashed on."""
    m = {"drift_log": "oops"}
    n = dc.narrate_drift(m, [_ev("remove", "cron", "x")], definition_status="defined")
    assert n == 1
    assert isinstance(m["drift_log"], list)


def test_count_unreviewed_drift() -> None:
    m = {"drift_log": [
        {"source": dc.DRIFT_LOG_SOURCE, "reviewed": False},
        {"source": dc.DRIFT_LOG_SOURCE, "reviewed": True},
        {"source": dc.DRIFT_LOG_SOURCE, "reviewed": False},
        # a foreign producer's entry is ignored
        {"source": "other", "reviewed": False},
        # the v7-arc forge change_log shape (no source/reviewed) is ignored
        {"entry_id": "e1", "kind": "capability_added", "who": "bot"},
    ]}
    assert dc.count_unreviewed_drift(m) == 2


def test_count_unreviewed_empty() -> None:
    assert dc.count_unreviewed_drift({}) == 0
    assert dc.count_unreviewed_drift({"drift_log": []}) == 0


def test_non_string_target_does_not_crash() -> None:
    """A hand-edited manifest may carry a non-string cron label / action id.
    Classification + narration must not raise (crash-safety contract)."""
    # is_executable_path / _extension tolerate non-str.
    assert dc.is_executable_path(123) is False
    # cron/action are behavioral regardless → major, and narration succeeds.
    m = {}
    n = dc.narrate_drift(
        m,
        [{"kind": "remove", "target_type": "cron", "target": 42},
         {"kind": "modify", "target_type": "action", "target": 7}],
        definition_status="defined",
    )
    assert n == 2
    assert all(isinstance(e["summary"], str) for e in m["drift_log"])


# ════════════════════════════════════════════════════════════════════════════
# Layer 3 — integration: the scanner Phase-6 sequence over real fixtures
#
# Mirrors scanner.py Phase 6: apply_reconciliation(manifest, workspace) then
# narrate_drift(manifest, summary.drift_events, definition_status=...).
# ════════════════════════════════════════════════════════════════════════════

def _phase6(manifest: dict, workspace: Path, **recon_kw) -> int:
    """Replicate the scanner Phase-6 reconcile→narrate sequence. Returns the
    number of drift_log entries appended."""
    summary = apply_reconciliation(manifest, workspace, **recon_kw)
    return dc.narrate_drift(
        manifest, summary.drift_events,
        definition_status=manifest.get("definition_status", ""),
        llm_fn=None,
    )


def test_proof_new_script_major_on_defined(tmp_path: Path) -> None:
    """A claimed script vanished from disk on a DEFINED app → MAJOR log entry."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    manifest = {
        "id": "habit-tracker", "name": "Habit Tracker",
        "definition_status": "defined",
        "files": [{"path": "scripts/reminder.py", "layer": "code"}],
    }
    appended = _phase6(manifest, ws)
    assert appended == 1
    entry = manifest["drift_log"][0]
    assert entry["target"] == "scripts/reminder.py"
    assert entry["kind"] == "remove"
    assert entry["significance"] == "major"
    # And the reconcile pass still absorbed it (files[] refreshed).
    assert manifest["files"] == []


def test_proof_new_md_silent_no_entry(tmp_path: Path) -> None:
    """A data/doc file drift on a DEFINED app → silent absorb, NO entry."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    manifest = {
        "id": "journal", "name": "Journal", "definition_status": "defined",
        "files": [{"path": "data/2026-06-24.md", "layer": "data"}],
    }
    appended = _phase6(manifest, ws)
    assert appended == 0
    assert manifest.get("drift_log", []) == []
    assert manifest["files"] == []          # still absorbed


def test_proof_removed_cron_major(tmp_path: Path) -> None:
    """A claimed cron not in the live list on a DEFINED app → MAJOR."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    manifest = {
        "id": "backup", "name": "Backup", "definition_status": "defined",
        "crons": [{"label": "nightly-backup", "schedule": "0 3 * * *",
                   "script": "scripts/backup.sh"}],
    }
    appended = _phase6(manifest, ws, live_cron_labels=set())
    assert appended == 1
    entry = manifest["drift_log"][0]
    assert entry["target"] == "nightly-backup"
    assert entry["target_type"] == "cron"
    assert entry["kind"] == "remove"


def test_proof_discovered_app_no_log(tmp_path: Path) -> None:
    """The SAME drift on a DISCOVERED app → fresh content, NO narrative."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    manifest = {
        "id": "draft", "name": "Draft", "definition_status": "discovered",
        "files": [{"path": "scripts/reminder.py", "layer": "code"}],
        "crons": [{"label": "nightly", "schedule": "0 0 * * *",
                   "script": "scripts/n.sh"}],
    }
    appended = _phase6(manifest, ws, live_cron_labels=set())
    assert appended == 0
    assert manifest.get("drift_log", []) == []
    # Content still refreshed (the scanner is a guesser for discovered apps).
    assert manifest["files"] == []
    assert manifest["crons"] == []


def test_proof_action_anchor_modify_major(tmp_path: Path) -> None:
    """A scheduled action whose evidence anchor moved (authored field) →
    a MODIFY event, classified MAJOR (behavioral surface)."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "HEARTBEAT.md").write_text("# Heartbeat\n\nUnrelated content.")
    manifest = {
        "id": "coach", "name": "Coach", "definition_status": "defined",
        "provenance": {"field_origins": {
            "scheduled_actions": {"source": "forge_built"}}},
        "scheduled_actions": [{
            "id": "protein", "state": "active", "quality": "extracted",
            "trigger": {"kind": "heartbeat",
                        "evidence_path": "HEARTBEAT.md",
                        "evidence_locator": "Protein Reminder Section"},
        }],
    }
    appended = _phase6(manifest, ws)
    assert appended == 1
    entry = manifest["drift_log"][0]
    assert entry["target_type"] == "action"
    assert entry["kind"] == "modify"
    assert entry["target"] == "protein"


def test_proof_extra_file_script_add_major(tmp_path: Path) -> None:
    """A new un-claimed script matched by a volatile_paths glob on a DEFINED
    app → an ADD event, MAJOR."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    manifest = {
        "id": "tool", "name": "Tool", "definition_status": "defined",
        "files": [],
        "volatile_paths": [{"glob": "plugins/*.py", "layer": "code"}],
    }
    appended = _phase6(manifest, ws, workspace_files={"plugins/extra.py"})
    assert appended == 1
    entry = manifest["drift_log"][0]
    assert entry["kind"] == "add"
    assert entry["target"] == "plugins/extra.py"


def test_phase6_idempotent_across_scans(tmp_path: Path) -> None:
    """After the first scan absorbs observational drift, a second scan finds
    nothing to narrate (the delta is gone)."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    manifest = {
        "id": "x", "name": "X", "definition_status": "defined",
        "files": [{"path": "scripts/gone.py", "layer": "code"}],
    }
    assert _phase6(manifest, ws) == 1
    assert _phase6(manifest, ws) == 0       # absorbed → no recurrence
    assert len(manifest["drift_log"]) == 1
