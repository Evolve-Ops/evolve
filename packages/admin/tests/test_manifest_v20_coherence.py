"""Schema v20 migration tests — coherence + reconciliation foundation.

Validates the schema migration described in
internal/spec-app-coherence-and-reconciliation-2026-06-05.md §3.1, §3.4, §4,
§13.1. v20 adds four top-level blocks (`provenance`, `reconciliation`,
`coherence`, `volatile_paths`) and three per-entry fields on
scheduled_actions (`state`, `quality`, `safety_net_for`), plus the
`script` → `code` layer rename on files[*].

The migration is purely additive — no existing field is overwritten,
no existing semantics change. Behavior is gated on PR 4+ reading these
fields; PR 1 ships the data shape.

Pinned against the running production schema as observed across personal-bot,
team-bot-a, team-bot-c, security-bot, and evolve (5-bot validation in spec §17). Tests
cover both the "fresh v19 manifest" path (full migration) and the
"already-v20" path (idempotency).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.manifest import (  # noqa: E402
    MANIFEST_SCHEMA_VERSION,
    MECHANISM_UNKNOWN,
    _migrate_file_entry_v20,
    _migrate_scheduled_action_entry_v20,
    _populate_provenance_v20,
    migrate_manifest,
)


# Minimal v19-shape manifest used as the migration starting point.
# Mirrors personal-bot's `app-daily-cost-check.json` skeleton — bot-id present,
# source is "discovered" (observational), no build_spec (not forge-built),
# a couple of files at the legacy `script` layer.
def _v19_manifest_baseline() -> dict:
    return {
        "id": "daily-cost-check",
        "name": "Daily Cost Check",
        "bot_id": "personal-bot",
        "description": "Monitor workspace cost overruns and alert the operator via Telegram.",
        "source": "discovered",
        "schema_version": 19,
        "manifest_type": "evolve_application",
        "created_at": "2026-05-23T07:42:28Z",
        "updated_at": "2026-05-27T08:27:42Z",
        "status": "active",
        "files": [
            {"file_id": "f-001", "path": "scripts/check-daily-cost.py", "layer": "script"},
            {"file_id": "f-002", "path": "HEARTBEAT.md", "layer": "state"},
        ],
        "scheduled_actions": [
            {
                "id": "cost-check-daily",
                "trigger": {"kind": "heartbeat", "schedule": "daily",
                            "evidence_path": "HEARTBEAT.md"},
                "summary": "Run check-daily-cost.py; alert if > $20.",
                "mechanism": "oc_heartbeat_instruction",
                "install": {},
            },
            {
                "id": "ambiguous-action",
                "trigger": {"kind": "heartbeat", "schedule": "daily",
                            "evidence_path": "AGENTS.md"},
                "summary": "1. Read SOUL.md — who you are",
                "mechanism": MECHANISM_UNKNOWN,
                "install": {},
            },
        ],
        # All other v19 fields elided — migrate_manifest fills defaults.
    }


def _write(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / f"{data['id']}.json"
    p.write_text(json.dumps(data))
    return p


# ── Schema version pin ─────────────────────────────────────────────────────

def test_schema_version_is_30() -> None:
    """Cross-check the version constant. The pin in
    test_manifest_v13_scheduled_actions.py covers the same assertion; this
    test exists so the v20 test file fails clearly if someone bumps the
    schema without updating both files.
    """
    assert MANIFEST_SCHEMA_VERSION == 30


# ── New top-level blocks ───────────────────────────────────────────────────

def test_migrate_v19_adds_provenance_block(tmp_path: Path) -> None:
    """A v19 manifest gets a populated provenance block after migration."""
    path = _write(tmp_path, _v19_manifest_baseline())
    migrate_manifest(path)
    data = json.loads(path.read_text())

    assert "provenance" in data
    prov = data["provenance"]
    # Source "discovered" with no build_spec → observational manifest.
    assert prov["manifest_origin"] == "observational"
    assert prov["created_by"] == "scanner"
    assert prov["created_at"] == "2026-05-23T07:42:28Z"
    assert prov["last_promoted_at"] is None
    # field_origins stamps every top-level field present at migration time.
    assert isinstance(prov["field_origins"], dict)
    assert prov["field_origins"]["description"]["source"] == "observational"
    assert prov["field_origins"]["files"]["source"] == "observational"
    assert prov["field_origins"]["scheduled_actions"]["source"] == "observational"


def test_migrate_v19_forge_built_provenance_via_source(tmp_path: Path) -> None:
    """A v19 manifest with source='forge_built' gets forge_built provenance."""
    manifest = _v19_manifest_baseline()
    manifest["source"] = "forge_built"
    path = _write(tmp_path, manifest)
    migrate_manifest(path)
    data = json.loads(path.read_text())

    prov = data["provenance"]
    assert prov["manifest_origin"] == "forge_built"
    # Forge-built default applies to every field uniformly per §13.1.
    # Per-field granularity is a v2 evolution; v20 uses uniform per-manifest.
    assert prov["field_origins"]["scheduled_actions"]["source"] == "forge_built"
    assert prov["field_origins"]["description"]["source"] == "forge_built"


def test_migrate_v19_forge_built_provenance_via_install_job(tmp_path: Path) -> None:
    """A v19 manifest with install_job populated gets forge_built provenance.

    Forge stamps install_job when it dispatches an install — a reliable
    signal even when source remains 'discovered' (which can happen on
    manifests that were forge-built once and then re-scanned).
    """
    manifest = _v19_manifest_baseline()
    manifest["install_job"] = {"job_id": "j-abc123", "started_at": "2026-06-01T10:00:00Z"}
    path = _write(tmp_path, manifest)
    migrate_manifest(path)
    data = json.loads(path.read_text())

    assert data["provenance"]["manifest_origin"] == "forge_built"


def test_migrate_v19_build_spec_alone_does_not_imply_forge(tmp_path: Path) -> None:
    """A v19 manifest with a populated build_spec but source='discovered' is
    OBSERVATIONAL, not forge_built. Empirically, the scanner stuffs generated
    build documentation into build_spec — so a populated value alone is
    not a reliable forge-built signal. Validation against personal-bot's
    app-daily-cost-check.json surfaced this — its build_spec contains
    1800+ chars of scanner-generated docs, but source is 'discovered'.

    This is the load-bearing safety check: without it, every scanner-
    discovered app with rich descriptions would migrate as forge_built
    and immediately surface authored-field chips on day 1.
    """
    manifest = _v19_manifest_baseline()
    manifest["build_spec"] = "## File Layout\n- scripts/foo.py — Main entry point\n..."
    # source stays "discovered" from baseline.
    path = _write(tmp_path, manifest)
    migrate_manifest(path)
    data = json.loads(path.read_text())

    # build_spec ignored; source="discovered" wins → observational.
    assert data["provenance"]["manifest_origin"] == "observational"


def test_migrate_v19_adds_reconciliation_block(tmp_path: Path) -> None:
    """A v19 manifest gets an empty-ok reconciliation block."""
    path = _write(tmp_path, _v19_manifest_baseline())
    migrate_manifest(path)
    data = json.loads(path.read_text())

    rec = data["reconciliation"]
    assert rec["status"] == "ok"
    assert rec["last_reconciled_at"] is None
    assert rec["extra_files"] == []
    assert rec["missing_files"] == []
    assert rec["missing_crons"] == []
    assert rec["missing_actions"] == []
    assert rec["volatile_growth_anomalies"] == []
    assert rec["operator_decisions"] == []


def test_migrate_v19_adds_coherence_block(tmp_path: Path) -> None:
    """A v19 manifest gets an empty-ok coherence block."""
    path = _write(tmp_path, _v19_manifest_baseline())
    migrate_manifest(path)
    data = json.loads(path.read_text())

    coh = data["coherence"]
    assert coh["status"] == "ok"
    assert coh["last_checked_at"] is None
    assert coh["findings"] == []
    assert coh["last_capability_check"] is None
    assert coh["coherence_accepted"] == []


def test_migrate_v19_adds_volatile_paths_empty(tmp_path: Path) -> None:
    """A v19 manifest gets an empty volatile_paths[] list."""
    path = _write(tmp_path, _v19_manifest_baseline())
    migrate_manifest(path)
    data = json.loads(path.read_text())
    assert data["volatile_paths"] == []


# ── Per-scheduled_action migration ─────────────────────────────────────────

def test_migrate_v19_scheduled_action_state_default_active(tmp_path: Path) -> None:
    """Existing scheduled_actions[] entries get state=active by default."""
    path = _write(tmp_path, _v19_manifest_baseline())
    migrate_manifest(path)
    data = json.loads(path.read_text())

    for action in data["scheduled_actions"]:
        assert action["state"] == "active"


def test_migrate_v19_quality_unknown_marked_suspect(tmp_path: Path) -> None:
    """Entries with mechanism='unknown' get quality='suspect'.

    The dominant case for personal-bot and team-bot-a — first-line excerpts of AGENTS.md
    sections captured by the legacy scanner as if they were scheduled
    behaviors. Coherence Pass A skips these to avoid false-positive flooding.
    """
    path = _write(tmp_path, _v19_manifest_baseline())
    migrate_manifest(path)
    data = json.loads(path.read_text())

    by_id = {a["id"]: a for a in data["scheduled_actions"]}
    # mechanism: MECHANISM_UNKNOWN ("unknown") → suspect
    assert by_id["ambiguous-action"]["quality"] == "suspect"


def test_migrate_v19_quality_known_marked_extracted(tmp_path: Path) -> None:
    """Entries with a real mechanism get quality='extracted'."""
    path = _write(tmp_path, _v19_manifest_baseline())
    migrate_manifest(path)
    data = json.loads(path.read_text())

    by_id = {a["id"]: a for a in data["scheduled_actions"]}
    # mechanism: "oc_heartbeat_instruction" → extracted
    assert by_id["cost-check-daily"]["quality"] == "extracted"


def test_migrate_v19_safety_net_for_default_empty(tmp_path: Path) -> None:
    """All existing entries get safety_net_for=[]."""
    path = _write(tmp_path, _v19_manifest_baseline())
    migrate_manifest(path)
    data = json.loads(path.read_text())

    for action in data["scheduled_actions"]:
        assert action["safety_net_for"] == []


def test_migrate_v19_missing_mechanism_treated_as_suspect(tmp_path: Path) -> None:
    """Entries with no mechanism field at all are treated as suspect.

    Belt-and-suspenders for the case where a manifest somehow has a
    scheduled_action without mechanism set. v16 migration should have
    populated mechanism, but if it didn't, the safer default for
    quality is suspect.
    """
    manifest = _v19_manifest_baseline()
    manifest["scheduled_actions"] = [
        {"id": "weird", "trigger": {"kind": "heartbeat"}, "summary": "..."}
        # No mechanism field at all.
    ]
    path = _write(tmp_path, manifest)
    migrate_manifest(path)
    data = json.loads(path.read_text())

    assert data["scheduled_actions"][0]["quality"] == "suspect"


# ── Per-file migration (layer rename) ──────────────────────────────────────

def test_migrate_v19_layer_script_maps_to_code(tmp_path: Path) -> None:
    """files[*].layer == 'script' gets mapped to 'code'."""
    path = _write(tmp_path, _v19_manifest_baseline())
    migrate_manifest(path)
    data = json.loads(path.read_text())

    # First file was layer="script"; should now be "code" with legacy capture.
    f0 = data["files"][0]
    assert f0["layer"] == "code"
    assert f0["_legacy_layer"] == "script"


def test_migrate_v19_layer_other_values_unchanged(tmp_path: Path) -> None:
    """Other layer values are NOT remapped. Only 'script' has a clean
    mapping. Mis-classifications like HEARTBEAT.md tagged 'state' instead
    of 'behavior_doc' are handled by PR 3's classifier, not by this
    migration.
    """
    path = _write(tmp_path, _v19_manifest_baseline())
    migrate_manifest(path)
    data = json.loads(path.read_text())

    # HEARTBEAT.md was layer="state" in the v19 fixture; stays "state"
    # for now. PR 3's classifier will re-classify.
    f1 = data["files"][1]
    assert f1["layer"] == "state"
    assert "_legacy_layer" not in f1  # only set when remapped


# ── Schema version stamp ───────────────────────────────────────────────────

def test_migrate_v19_bumps_schema_version_to_current(tmp_path: Path) -> None:
    """Migration stamps schema_version up to the current constant."""
    manifest = _v19_manifest_baseline()
    path = _write(tmp_path, manifest)
    migrate_manifest(path)
    data = json.loads(path.read_text())
    assert data["schema_version"] == MANIFEST_SCHEMA_VERSION


# ── Idempotency ────────────────────────────────────────────────────────────

def test_migrate_v20_is_idempotent(tmp_path: Path) -> None:
    """Migrating a v20 manifest is a no-op (content unchanged)."""
    path = _write(tmp_path, _v19_manifest_baseline())
    # First migration brings it to v20.
    migrate_manifest(path)
    first = path.read_text()
    # Second migration should not modify the file.
    migrate_manifest(path)
    second = path.read_text()
    assert first == second


def test_migrate_v20_preserves_existing_v20_values(tmp_path: Path) -> None:
    """If a manifest already has populated v20 blocks (e.g., the operator
    promoted a field to user_authored), the migration MUST NOT overwrite
    those values. This is the load-bearing invariant of the additive
    migration policy.
    """
    manifest = _v19_manifest_baseline()
    # Pretend the operator has already promoted description to authored.
    manifest["schema_version"] = 20
    manifest["provenance"] = {
        "manifest_origin": "observational",
        "created_by": "scanner",
        "created_at": "2026-05-23T07:42:28Z",
        "last_promoted_at": "2026-06-01T10:00:00Z",
        "field_origins": {
            "description": {"source": "user_authored", "by": "operator",
                            "at": "2026-06-01T10:00:00Z"},
            "files": {"source": "observational"},
        },
    }
    path = _write(tmp_path, manifest)
    migrate_manifest(path)
    data = json.loads(path.read_text())

    # The hand-authored description provenance survives the migration.
    desc_origin = data["provenance"]["field_origins"]["description"]
    assert desc_origin["source"] == "user_authored"
    assert desc_origin["by"] == "operator"
    assert desc_origin["at"] == "2026-06-01T10:00:00Z"
    # The observational files origin also survives.
    assert data["provenance"]["field_origins"]["files"]["source"] == "observational"


# ── Helper unit tests ──────────────────────────────────────────────────────

def test_migrate_scheduled_action_entry_v20_helper_idempotent() -> None:
    """The per-entry helper is idempotent (returns False on the second call)."""
    entry = {"id": "x", "mechanism": MECHANISM_UNKNOWN}
    assert _migrate_scheduled_action_entry_v20(entry) is True
    assert _migrate_scheduled_action_entry_v20(entry) is False


def test_migrate_file_entry_v20_helper_idempotent() -> None:
    """The per-file helper is idempotent."""
    entry = {"file_id": "f-1", "layer": "script", "path": "scripts/foo.py"}
    assert _migrate_file_entry_v20(entry) is True
    assert _migrate_file_entry_v20(entry) is False  # already "code"
    assert entry["layer"] == "code"


def test_populate_provenance_v20_skips_meta_fields() -> None:
    """Meta fields (schema_version, created_at, updated_at, the new v20
    blocks themselves) are excluded from field_origins. They are not
    operator-authored content; they are infrastructure."""
    data = {
        "id": "test",
        "description": "hi",
        "schema_version": 20,
        "created_at": "...",
        "updated_at": "...",
        "reconciliation": {},
        "coherence": {},
        "volatile_paths": [],
        "provenance": {},
    }
    _populate_provenance_v20(data)
    field_origins = data["provenance"]["field_origins"]

    # Real fields stamped.
    assert "description" in field_origins
    # Meta fields excluded.
    assert "schema_version" not in field_origins
    assert "created_at" not in field_origins
    assert "updated_at" not in field_origins
    assert "reconciliation" not in field_origins
    assert "coherence" not in field_origins
    assert "volatile_paths" not in field_origins
    assert "provenance" not in field_origins


# ── Existing-fields-preserved invariant ────────────────────────────────────

def test_migrate_v19_preserves_all_existing_fields(tmp_path: Path) -> None:
    """The migration must not delete or modify any existing field's value.

    This is the most important invariant in §13.1 — the migration is
    purely additive. A regression here would silently change production
    manifests on next admin tick.
    """
    manifest = _v19_manifest_baseline()
    # Add some rich v19 fields to make sure they survive.
    manifest["identity"] = {"purpose": "Hand-written purpose",
                            "scope_includes": ["a", "b"]}
    manifest["requirements"] = {"integrations": ["slack"]}
    manifest["capability_tags"] = ["budget", "alerting"]
    manifest["confidence"] = 0.85

    path = _write(tmp_path, manifest)
    migrate_manifest(path)
    data = json.loads(path.read_text())

    # Every original value survived exactly.
    assert data["identity"]["purpose"] == "Hand-written purpose"
    assert data["identity"]["scope_includes"] == ["a", "b"]
    assert data["requirements"]["integrations"] == ["slack"]
    assert data["capability_tags"] == ["budget", "alerting"]
    assert data["confidence"] == 0.85
    # And the original scheduled_actions structure (now with state/quality
    # added) keeps its substantive content.
    by_id = {a["id"]: a for a in data["scheduled_actions"]}
    assert by_id["cost-check-daily"]["summary"] == \
        "Run check-daily-cost.py; alert if > $20."
    assert by_id["cost-check-daily"]["mechanism"] == "oc_heartbeat_instruction"
