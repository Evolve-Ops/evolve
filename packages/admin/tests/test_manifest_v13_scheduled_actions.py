"""Schema v13 migration + scanner scheduled-action extraction tests.

Workstream A of the audit-extensions sprint
(docs/spec-audit-extensions-2026-05-17.md §3). Verifies that:

- migrate_manifest brings a v11 / v12 manifest forward to v13 with the
  new scheduled_actions[] / heartbeat_evidence / cron_evidence fields
  populated as empty defaults.
- The scanner's _extract_scheduled_action_candidates picks up
  schedule-hinted sections from HEARTBEAT.md / AGENTS.md and produces
  candidates with the right anchor + sha shape.
- _split_markdown_sections + _build_scheduled_action_entry round-trip
  cleanly so a heartbeat section yields a manifest scheduled_actions[]
  entry whose Tier-2 anchor + drift checks pass.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.manifest import (  # noqa: E402
    MANIFEST_SCHEMA_VERSION,
    MECHANISM_OC_HEARTBEAT_HOOK,
    MECHANISM_OC_HEARTBEAT_INSTRUCTION,
    MECHANISM_OC_SESSION_HOOK,
    MECHANISM_OC_SESSION_INSTRUCTION,
    MECHANISM_UNKNOWN,
    SCHEDULED_ACTION_MECHANISMS,
    _DEPRECATED_MECHANISMS,
    _migrate_scheduled_action_entry_v16,
    _migrate_scheduled_action_entry_v17,
    migrate_manifest,
)
from evolve_admin.applications import scanner as scanner_mod  # noqa: E402


# ── migrate_manifest brings older schemas forward ───────────────────────────


def test_schema_version_is_28() -> None:
    """Pinned to the current MANIFEST_SCHEMA_VERSION so version bumps surface here.

    v21/v22 (freelance-bypass fields, workspace file sync) shipped without
    bumping the constant; manifest-v7 Slice 1 re-synced it to 22
    (spec-manifest-v7-slicing-2026-06-10.md §2.3) and added the
    label-vs-constant guard in test_manifest_schema_version_guard.py.
    v23 adds the optional delivery_contract{} sub-block on
    scheduled_actions[] entries (proactive-delivery monitor, U2.1 —
    docs/spec-proactive-delivery-monitor-2026-06-10.md §5).
    v24 adds the privacy{} + audience_scoping{} top-level blocks
    (manifest-v7 Slice 2 — spec-manifest-v7-slicing-2026-06-10.md §4). See
    test_manifest_v24_privacy_scoping.py.
    v25 adds app_kind + classification{} — the purpose/fit classifier
    (app-scan bite D1) labels manifests goal-application vs capability.
    v26 widens the app_kind enum with "system" (pod/agent-runtime
    infrastructure) and stamps classifier_version into classification{} —
    Scanner Slice 2.
    v27 adds definition_status — the Defined/Discovered source-of-truth axis
    (Bite 1; docs/spec-apps-meta-2026-06-13.md §9). See
    test_manifest_definition_status.py.
    v28 adds drift_log — the drift-narrative log (Bite 3;
    docs/spec-apps-meta-2026-06-13.md §9.3). See test_drift_classifier.py.
    """
    assert MANIFEST_SCHEMA_VERSION == 28


def test_migrate_v11_adds_v13_fields_with_empty_defaults(tmp_path: Path) -> None:
    """A v11 manifest gets the v13 scheduled-action fields populated empty."""
    path = tmp_path / "journal.json"
    v11 = {
        "id": "journal",
        "name": "Journal",
        "bot_id": "team_bot_a",
        "schema_version": 11,
        # v11 fields present:
        "last_structural_verify": {},
        "audit_trail_path": "",
    }
    path.write_text(json.dumps(v11))
    migrate_manifest(path)
    after = json.loads(path.read_text())
    assert after["schema_version"] == MANIFEST_SCHEMA_VERSION
    # v13 fields exist, defaulted empty
    assert after["scheduled_actions"] == []
    assert after["heartbeat_evidence"] == {}
    assert after["cron_evidence"] == {}
    # v11 fields preserved
    assert after["last_structural_verify"] == {}


def test_migrate_v12_preserves_existing_audit_fields(tmp_path: Path) -> None:
    """v12-specific fields aren't disturbed when v13 fields are added."""
    path = tmp_path / "x.json"
    v12 = {
        "id": "x",
        "name": "X",
        "bot_id": "admin_bot",
        "schema_version": 12,
        "audit_cadence": "weekly",
        "audit_eligible": True,
        "audit_accepted": [{"signature": "sig1", "accepted_at": "2026-05-01"}],
        "last_audit": {"verified_at": "2026-05-10"},
    }
    path.write_text(json.dumps(v12))
    migrate_manifest(path)
    after = json.loads(path.read_text())
    assert after["schema_version"] == MANIFEST_SCHEMA_VERSION
    # v12 fields untouched
    assert after["audit_cadence"] == "weekly"
    assert after["audit_eligible"] is True
    assert after["audit_accepted"] == [{"signature": "sig1", "accepted_at": "2026-05-01"}]
    assert after["last_audit"] == {"verified_at": "2026-05-10"}
    # v13 fields added
    assert "scheduled_actions" in after
    assert "heartbeat_evidence" in after
    assert "cron_evidence" in after


def test_migrate_v13_is_idempotent(tmp_path: Path) -> None:
    """Re-migrating a v13 manifest doesn't double-add or strip fields.

    The original v13 payload is preserved, plus the v16 install-metadata
    sub-fields are added to each scheduled_actions entry (see the
    dedicated v16 tests below for the per-entry shape).
    """
    path = tmp_path / "y.json"
    initial = {
        "id": "y",
        "name": "Y",
        "bot_id": "team_bot_b",
        "schema_version": 13,
        "scheduled_actions": [{"id": "wakeup-7am", "trigger": {"kind": "heartbeat"}}],
        "heartbeat_evidence": {"file_path": "HEARTBEAT.md", "section_anchors": ["Morning"]},
        "cron_evidence": {},
    }
    path.write_text(json.dumps(initial))
    migrate_manifest(path)
    after = json.loads(path.read_text())
    assert after["schema_version"] == MANIFEST_SCHEMA_VERSION
    # Original payload preserved.
    assert after["scheduled_actions"][0]["id"] == "wakeup-7am"
    assert after["scheduled_actions"][0]["trigger"] == {"kind": "heartbeat"}
    # v16 sub-fields added in-place.
    assert after["scheduled_actions"][0]["mechanism"] == MECHANISM_UNKNOWN
    assert after["scheduled_actions"][0]["install"] == {}
    assert after["heartbeat_evidence"]["section_anchors"] == ["Morning"]


# ── v15: per-app data classification for cloud backup ──────────────────────


def test_migrate_v14_adds_v15_fields_empty(tmp_path: Path) -> None:
    """A v14 manifest gets the v15 backup-classification fields populated empty.

    Empty/absent means "no classification declared" — the resolver treats
    that as "everything cloud-eligible" so existing v14 manifests keep
    backing up exactly as before.
    """
    path = tmp_path / "journal.json"
    v14 = {
        "id": "journal",
        "name": "Journal",
        "bot_id": "team_bot_a",
        "schema_version": 14,
        "manifest_shape": "",
    }
    path.write_text(json.dumps(v14))
    migrate_manifest(path)
    after = json.loads(path.read_text())
    assert after["schema_version"] == MANIFEST_SCHEMA_VERSION
    assert after["app_files_privacy"] == ""
    assert after["data_paths"] == []
    assert after["default_for_unclassified"] == ""


def test_migrate_v15_preserves_existing_classification(tmp_path: Path) -> None:
    """When a v15 manifest already has classification declared, migrate leaves it alone."""
    path = tmp_path / "notes.json"
    v15 = {
        "id": "notes",
        "name": "Notes",
        "bot_id": "evo",
        "schema_version": 15,
        "app_files_privacy": "cloud",
        "data_paths": [
            {"path": "notes/", "privacy": "local", "note": "user-authored"},
            {"path": "cache/", "privacy": "ephemeral"},
        ],
        "default_for_unclassified": "local",
    }
    path.write_text(json.dumps(v15))
    migrate_manifest(path)
    after = json.loads(path.read_text())
    assert after["schema_version"] == MANIFEST_SCHEMA_VERSION
    assert after["app_files_privacy"] == "cloud"
    assert after["data_paths"][0]["path"] == "notes/"
    assert after["data_paths"][0]["privacy"] == "local"
    assert after["data_paths"][1]["privacy"] == "ephemeral"
    assert after["default_for_unclassified"] == "local"


def test_v15_fields_round_trip_through_from_dict(tmp_path: Path) -> None:
    """ApplicationManifest.from_dict accepts the v15 fields cleanly."""
    from evolve_admin.applications.manifest import ApplicationManifest
    data = {
        "id": "x", "name": "X", "bot_id": "team_bot_a",
        "app_files_privacy": "local",
        "data_paths": [{"path": "logs/", "privacy": "ephemeral"}],
        "default_for_unclassified": "cloud",
    }
    m = ApplicationManifest.from_dict(data)
    assert m.app_files_privacy == "local"
    assert m.data_paths == [{"path": "logs/", "privacy": "ephemeral"}]
    assert m.default_for_unclassified == "cloud"


def test_v15_field_defaults_when_omitted() -> None:
    """A bare manifest dataclass has the v15 fields defaulted empty."""
    from evolve_admin.applications.manifest import ApplicationManifest
    m = ApplicationManifest(id="x", name="X", bot_id="team_bot_a")
    assert m.app_files_privacy == ""
    assert m.data_paths == []
    assert m.default_for_unclassified == ""


# ── v16: scheduled_action install metadata ───────────────────────────────────


def test_mechanism_enum_includes_oc_hooks_and_launchd() -> None:
    """The mechanism vocabulary covers the install surfaces forge supports.

    v17 preferred values: oc_heartbeat_instruction / oc_session_instruction.
    The deprecated oc_heartbeat_hook / oc_session_hook stay in the enum
    for one version so migration can detect them.
    """
    # v17 preferred surfaces
    assert "oc_heartbeat_instruction" in SCHEDULED_ACTION_MECHANISMS
    assert "oc_session_instruction" in SCHEDULED_ACTION_MECHANISMS
    # Deprecated (kept for v17 migration; removed in v18)
    assert "oc_heartbeat_hook" in SCHEDULED_ACTION_MECHANISMS
    assert "oc_session_hook" in SCHEDULED_ACTION_MECHANISMS
    # Out-of-session fallbacks
    assert "launchd" in SCHEDULED_ACTION_MECHANISMS
    assert "crontab" in SCHEDULED_ACTION_MECHANISMS
    # Edge cases
    assert "external" in SCHEDULED_ACTION_MECHANISMS
    assert MECHANISM_UNKNOWN in SCHEDULED_ACTION_MECHANISMS


def test_migrate_scheduled_action_entry_v16_backfills_unknown_mechanism() -> None:
    """Pre-v16 entries (no mechanism) get tagged 'unknown' so downstream code
    can distinguish 'scanner hasn't attributed yet' from genuine 'external'."""
    entry: dict = {"id": "wakeup-7am", "trigger": {"kind": "heartbeat"}}
    changed = _migrate_scheduled_action_entry_v16(entry)
    assert changed is True
    assert entry["mechanism"] == MECHANISM_UNKNOWN
    assert entry["install"] == {}


def test_migrate_scheduled_action_entry_v16_idempotent() -> None:
    """Re-running on an already-migrated entry is a no-op."""
    entry: dict = {
        "id": "task-check",
        "mechanism": MECHANISM_OC_HEARTBEAT_HOOK,
        "install": {"command": "python3 scripts/tasks.py check"},
    }
    changed = _migrate_scheduled_action_entry_v16(entry)
    assert changed is False
    # Existing mechanism/install preserved exactly
    assert entry["mechanism"] == MECHANISM_OC_HEARTBEAT_HOOK
    assert entry["install"] == {"command": "python3 scripts/tasks.py check"}


def test_migrate_scheduled_action_entry_v16_does_not_stamp_provenance() -> None:
    """installed_at/installed_by are forge-stamped provenance, not migration
    defaults. They stay absent so consumers can distinguish 'never installed'
    from 'installed and we recorded it'."""
    entry: dict = {"id": "x", "trigger": {"kind": "heartbeat"}}
    _migrate_scheduled_action_entry_v16(entry)
    assert "installed_at" not in entry
    assert "installed_by" not in entry
    assert "installed_artifact" not in entry
    assert "last_verified" not in entry


def test_migrate_v15_manifest_backfills_v16_entry_fields(tmp_path: Path) -> None:
    """An end-to-end migration: a v15 manifest with a populated
    scheduled_actions[] gets each entry tagged with v16 install metadata."""
    path = tmp_path / "task-manager.json"
    v15 = {
        "id": "task-manager",
        "name": "Task Manager",
        "bot_id": "personal-bot",
        "schema_version": 15,
        "scheduled_actions": [
            {"id": "task-check", "trigger": {"kind": "heartbeat"}, "summary": "Surface overdue"},
            {"id": "archive-old", "trigger": {"kind": "launchd", "schedule": "weekly"}},
        ],
    }
    path.write_text(json.dumps(v15))
    migrate_manifest(path)
    after = json.loads(path.read_text())
    assert after["schema_version"] == MANIFEST_SCHEMA_VERSION
    # Each entry gained the v16 sub-fields.
    for entry in after["scheduled_actions"]:
        assert entry["mechanism"] == MECHANISM_UNKNOWN
        assert entry["install"] == {}
    # Entry payload preserved.
    assert after["scheduled_actions"][0]["id"] == "task-check"
    assert after["scheduled_actions"][0]["summary"] == "Surface overdue"
    assert after["scheduled_actions"][1]["trigger"]["schedule"] == "weekly"


def test_migrate_v15_manifest_with_empty_scheduled_actions_unchanged(tmp_path: Path) -> None:
    """A v15 manifest with no scheduled_actions entries doesn't sprout any."""
    path = tmp_path / "x.json"
    v15 = {"id": "x", "name": "X", "bot_id": "team_bot_a",
           "schema_version": 15, "scheduled_actions": []}
    path.write_text(json.dumps(v15))
    migrate_manifest(path)
    after = json.loads(path.read_text())
    assert after["schema_version"] == MANIFEST_SCHEMA_VERSION
    assert after["scheduled_actions"] == []


def test_migrate_handles_malformed_scheduled_action_entries(tmp_path: Path) -> None:
    """Defensive: a non-dict entry (corrupt data) doesn't crash the migration."""
    path = tmp_path / "x.json"
    v15 = {
        "id": "x", "name": "X", "bot_id": "team_bot_a",
        "schema_version": 15,
        "scheduled_actions": [
            "not-a-dict",                            # garbage from a bad scan
            {"id": "real", "trigger": {"kind": "heartbeat"}},
        ],
    }
    path.write_text(json.dumps(v15))
    migrate_manifest(path)  # should not raise
    after = json.loads(path.read_text())
    assert after["schema_version"] == MANIFEST_SCHEMA_VERSION
    # Real entry got the v16 fields; bogus entry left alone.
    assert after["scheduled_actions"][0] == "not-a-dict"
    assert after["scheduled_actions"][1]["mechanism"] == MECHANISM_UNKNOWN


# ── v17: oc_heartbeat_hook → oc_heartbeat_instruction migration ────────────


def test_deprecated_mechanisms_set_is_minimal() -> None:
    """The _DEPRECATED_MECHANISMS set names exactly the two values v17
    rewrites. Other mechanisms (launchd, crontab, etc.) are not
    deprecated."""
    assert _DEPRECATED_MECHANISMS == frozenset({
        "oc_heartbeat_hook", "oc_session_hook",
    })


def test_migrate_v17_rewrites_oc_heartbeat_hook(tmp_path: Path) -> None:
    """A v16 manifest carrying the deprecated mechanism gets it rewritten."""
    path = tmp_path / "x.json"
    v16 = {
        "id": "x", "name": "X", "bot_id": "personal-bot",
        "schema_version": 16,
        "scheduled_actions": [{
            "id": "task-check",
            "mechanism": MECHANISM_OC_HEARTBEAT_HOOK,
            "install": {"hook_event": "heartbeat", "command": "python3 scripts/tasks.py check"},
            "installed_at": "2026-06-01T00:00:00Z",
            "installed_by": "forge:j-XXXX",
            "installed_artifact": "openclaw.json#hooks.heartbeat[0]",
        }],
    }
    path.write_text(json.dumps(v16))
    migrate_manifest(path)
    after = json.loads(path.read_text())
    sa = after["scheduled_actions"][0]
    # Rewritten to the v17 mechanism.
    assert sa["mechanism"] == MECHANISM_OC_HEARTBEAT_INSTRUCTION
    # Bogus install_artifact cleared so next forge re-materializes to
    # HEARTBEAT.md (not openclaw.json).
    assert sa["installed_artifact"] is None
    assert sa["installed_at"] is None
    # installed_by preserved for audit trail.
    assert sa["installed_by"] == "forge:j-XXXX"
    # install recipe preserved — Phase 4.5 will read .command for the new
    # HEARTBEAT.md install (and the dispatch path requires fresh
    # `file`/`section_anchor`/`body` fields, which the operator/gallery
    # republish provides).
    assert sa["install"]["command"] == "python3 scripts/tasks.py check"


def test_migrate_v17_rewrites_oc_session_hook(tmp_path: Path) -> None:
    """Same rewrite applies to oc_session_hook → oc_session_instruction."""
    path = tmp_path / "x.json"
    v16 = {
        "id": "x", "name": "X", "bot_id": "personal-bot",
        "schema_version": 16,
        "scheduled_actions": [{
            "id": "session-start-greeting",
            "mechanism": MECHANISM_OC_SESSION_HOOK,
            "install": {"hook_event": "session_start", "command": "echo hi"},
        }],
    }
    path.write_text(json.dumps(v16))
    migrate_manifest(path)
    after = json.loads(path.read_text())
    assert after["scheduled_actions"][0]["mechanism"] == MECHANISM_OC_SESSION_INSTRUCTION


def test_migrate_v17_leaves_other_mechanisms_alone(tmp_path: Path) -> None:
    """launchd, crontab, external, unknown — none are rewritten."""
    path = tmp_path / "x.json"
    v16 = {
        "id": "x", "name": "X", "bot_id": "personal-bot",
        "schema_version": 16,
        "scheduled_actions": [
            {"id": "a", "mechanism": "launchd",  "install": {"plist_label": "com.x.y"}},
            {"id": "b", "mechanism": "crontab",  "install": {"label": "x"}},
            {"id": "c", "mechanism": "external", "install": {}},
            {"id": "d", "mechanism": "unknown",  "install": {}},
        ],
    }
    path.write_text(json.dumps(v16))
    migrate_manifest(path)
    after = json.loads(path.read_text())
    assert after["scheduled_actions"][0]["mechanism"] == "launchd"
    assert after["scheduled_actions"][1]["mechanism"] == "crontab"
    assert after["scheduled_actions"][2]["mechanism"] == "external"
    assert after["scheduled_actions"][3]["mechanism"] == "unknown"


def test_migrate_v17_already_v17_is_noop(tmp_path: Path) -> None:
    """A manifest already carrying the v17 mechanism doesn't get
    re-rewritten — _migrate_scheduled_action_entry_v17 returns False."""
    entry = {
        "id": "x",
        "mechanism": MECHANISM_OC_HEARTBEAT_INSTRUCTION,
        "install": {"file": "HEARTBEAT.md", "section_anchor": "## X",
                    "body": "x", "command": "x"},
        "installed_at": "2026-06-03T00:00:00Z",
        "installed_artifact": "HEARTBEAT.md#X",
    }
    assert _migrate_scheduled_action_entry_v17(entry) is False
    # Provenance preserved.
    assert entry["installed_artifact"] == "HEARTBEAT.md#X"


def test_migrate_v17_is_idempotent(tmp_path: Path) -> None:
    """Re-running migrate_manifest twice on a hook→instruction conversion
    doesn't double-rewrite or re-clear the (now-null) provenance."""
    path = tmp_path / "x.json"
    v16 = {
        "id": "x", "name": "X", "bot_id": "personal-bot",
        "schema_version": 16,
        "scheduled_actions": [{
            "id": "task-check",
            "mechanism": MECHANISM_OC_HEARTBEAT_HOOK,
            "install": {"command": "python3 scripts/tasks.py check"},
            "installed_artifact": "openclaw.json#hooks.heartbeat[0]",
        }],
    }
    path.write_text(json.dumps(v16))
    migrate_manifest(path)
    first = json.loads(path.read_text())
    migrate_manifest(path)  # second pass
    second = json.loads(path.read_text())
    assert first == second


def test_migrate_v17_handles_malformed_entries(tmp_path: Path) -> None:
    """A non-dict entry or empty mechanism doesn't crash migration."""
    path = tmp_path / "x.json"
    v16 = {
        "id": "x", "name": "X", "bot_id": "personal-bot",
        "schema_version": 16,
        "scheduled_actions": [
            "not-a-dict",
            {"id": "no_mechanism"},
            {"id": "empty_mechanism", "mechanism": ""},
            {"id": "real", "mechanism": MECHANISM_OC_HEARTBEAT_HOOK,
             "install": {"command": "x"}},
        ],
    }
    path.write_text(json.dumps(v16))
    migrate_manifest(path)  # should not raise
    after = json.loads(path.read_text())
    # Only the real heartbeat_hook entry got rewritten.
    assert after["scheduled_actions"][3]["mechanism"] == MECHANISM_OC_HEARTBEAT_INSTRUCTION


# ── _split_markdown_sections + extraction helpers ────────────────────────────


def test_split_markdown_sections_returns_heading_body_and_sha() -> None:
    text = (
        "# Top\n"
        "preamble\n"
        "## Daily routines\n"
        "Every evening at 6 PM, post a tally.\n"
        "## Weekly review\n"
        "Each Sunday at 9 AM.\n"
    )
    sections = scanner_mod._split_markdown_sections(text)
    headings = [s["heading"] for s in sections]
    assert "Daily routines" in headings
    assert "Weekly review" in headings
    daily = next(s for s in sections if s["heading"] == "Daily routines")
    assert "6 PM" in daily["body"]
    # sha matches body
    expected = hashlib.sha256(daily["body"].encode("utf-8")).hexdigest()
    assert daily["sha256"] == expected


def test_extract_scheduled_action_candidates_picks_protein_section(tmp_path: Path) -> None:
    """The scanner's pre-LLM extraction surfaces the protein-reminder section."""
    (tmp_path / "HEARTBEAT.md").write_text(
        "# Heartbeat\n\n"
        "## Daily routines\n\n"
        "Every evening at 6 PM, read `journal/protein.md` and post a tally\n"
        "to the user's messaging channel.\n\n"
        "## House style\n\nKeep tone friendly.\n"
    )
    candidates = scanner_mod._extract_scheduled_action_candidates(
        tmp_path, bot_id="admin_bot"
    )
    headings = [c["heading"] for c in candidates]
    assert "Daily routines" in headings, headings
    # The House style section has no schedule hint — should be skipped
    assert "House style" not in headings
    daily = next(c for c in candidates if c["heading"] == "Daily routines")
    assert daily["file_path"] == "HEARTBEAT.md"
    assert daily["section_sha256"], "candidate must carry a content sha"
    assert "protein" in daily["body"].lower()


def test_extract_handles_missing_files(tmp_path: Path) -> None:
    """No heartbeat / AGENTS / etc — returns empty list (no crash)."""
    assert scanner_mod._extract_scheduled_action_candidates(
        tmp_path, bot_id="empty"
    ) == []


def test_build_scheduled_action_entry_from_candidate(tmp_path: Path) -> None:
    """End-to-end: candidate dict → manifest scheduled_action entry."""
    body = (
        "Every evening at 6 PM, read `journal/protein.md` and post a tally\n"
        "to the user's messaging channel."
    )
    candidate = {
        "file_path": "HEARTBEAT.md",
        "heading": "Daily routines",
        "level": 2,
        "body": body,
        "section_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "line_start": 4,
        "line_end": 8,
    }
    entry = scanner_mod._build_scheduled_action_entry(candidate, app_name="Protein Reminder")
    assert entry["trigger"]["kind"] == "heartbeat"
    assert entry["trigger"]["evidence_path"] == "HEARTBEAT.md"
    assert entry["trigger"]["evidence_locator"] == "Daily routines"
    assert entry["trigger"]["section_sha256"] == candidate["section_sha256"]
    # Schedule string inferred from body
    assert "6 PM" in entry["trigger"]["schedule"] or "evening" in entry["trigger"]["schedule"].lower()
    # action id slugified
    assert "-" in entry["id"]
    # Summary is non-empty plain English
    assert entry["summary"]


def test_candidate_relates_to_app_by_name_token() -> None:
    """Heuristic match: 'Protein Reminder' fits the protein-mentioning section."""
    from evolve_admin.applications.scanner import DetectedApplication
    app = DetectedApplication(
        id="protein-reminder",
        name="Protein Reminder",
        confidence=0.9,
        evidence_files=["journal/protein.md"],
        evidence_summary="...",
        suggested_goals=[],
        suggested_tests=[],
        suggested_privacy=[],
    )
    candidate = {
        "file_path": "HEARTBEAT.md",
        "heading": "Daily routines",
        "body": "Every evening at 6 PM, read journal/protein.md and post a tally.",
    }
    assert scanner_mod._candidate_relates_to_app(candidate, app)


def test_candidate_does_not_relate_to_unrelated_app() -> None:
    from evolve_admin.applications.scanner import DetectedApplication
    app = DetectedApplication(
        id="weather",
        name="Weather Forecast",
        confidence=0.9,
        evidence_files=["data/weather.json"],
        evidence_summary="",
        suggested_goals=[],
        suggested_tests=[],
        suggested_privacy=[],
    )
    candidate = {
        "file_path": "HEARTBEAT.md",
        "heading": "Daily routines",
        "body": "Every evening at 6 PM, read journal/protein.md and post a tally.",
    }
    assert not scanner_mod._candidate_relates_to_app(candidate, app)


# ── LLM-prompt-regression: extraction with synthetic heartbeat ──────────────
#
# Without spinning up a real LLM call (which the scanner test infrastructure
# doesn't provide), we verify the prompt-input layer: given the same
# heartbeat / AGENTS content as the test pod, the pre-LLM extraction
# produces the right set of candidates. The downstream LLM prompt receives
# this list verbatim, so a correct extraction-input layer is the
# load-bearing thing for the prompt's behavior.


def test_extraction_layer_for_test_pod_shape(tmp_path: Path) -> None:
    """Mirrors a test-pod-shaped workspace; extraction picks up the
    schedule-mentioning sections from AGENTS.md and HEARTBEAT.md, ignores
    sections with no schedule hint.
    """
    (tmp_path / "AGENTS.md").write_text(
        "# AGENTS\n\n"
        "## Morning briefing\n\n"
        "Each morning at 7 AM, send a daily briefing with calendar + email.\n\n"
        "## House style\n\nKeep tone friendly and concise.\n"
    )
    (tmp_path / "HEARTBEAT.md").write_text(
        "# Heartbeat\n\n"
        "## Daily routines\n\n"
        "Every evening at 6 PM, post the protein tally.\n\n"
        "## On error\n\nLog and continue.\n"
    )
    candidates = scanner_mod._extract_scheduled_action_candidates(tmp_path, bot_id="admin_bot")
    headings = {(c["file_path"], c["heading"]) for c in candidates}
    assert ("AGENTS.md", "Morning briefing") in headings
    assert ("HEARTBEAT.md", "Daily routines") in headings
    # Non-schedule sections excluded
    assert ("AGENTS.md", "House style") not in headings
    assert ("HEARTBEAT.md", "On error") not in headings
