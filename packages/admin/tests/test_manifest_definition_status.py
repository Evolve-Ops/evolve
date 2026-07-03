"""Tests for the Defined/Discovered manifest lifecycle (schema v27, Bite 1).

Spec: docs/spec-apps-meta-2026-06-13.md §9.

Covers the five Bite-1 deliverables:
  1. Schema       — the inert `definition_status` field on both shapes.
  2. Born-status  — source → born definition_status mapping.
  3. Migration    — existing manifests land at "discovered" (no bulk promote).
  4. Promote/Demote — round-trip, idempotency, anchored-identity field marking,
                      and the v7-arc shape guard (no install-provenance damage).
  5. Existence    — a defined manifest is never L3-archived, even zero-file.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import manifest as m  # noqa: E402
from evolve_admin.applications.coherence_actions import (  # noqa: E402
    ANCHORED_IDENTITY_FIELDS,
    demote_to_discovered,
    promote_to_defined,
)
from evolve_admin.applications.reconciliation import is_field_authored  # noqa: E402
from evolve_admin.applications.scanner import (  # noqa: E402
    _archive_platform_file_only_stubs,
    _is_defined,
)


# ── 1. Schema ────────────────────────────────────────────────────────

def test_dataclass_default_is_discovered() -> None:
    """A freshly-constructed manifest is born "discovered" — the safe default:
    never accidentally vouched or existence-shielded."""
    mf = m.ApplicationManifest(id="x", name="X", bot_id="b")
    assert mf.definition_status == m.MANIFEST_DEFINITION_DISCOVERED
    assert "definition_status" in mf.to_dict()


def test_schema_version_bumped_for_field() -> None:
    # definition_status shipped at v27; this is a FLOOR, not a pin — later
    # field-adding slices (e.g. v28 drift_log, Bite 3) only raise the constant.
    # The generic "constant covers all labeled blocks" invariant is enforced by
    # test_manifest_schema_version_guard.py.
    assert m.MANIFEST_SCHEMA_VERSION >= 27


def test_from_dict_roundtrips_definition_status() -> None:
    mf = m.ApplicationManifest.from_dict(
        {"id": "y", "name": "Y", "bot_id": "b", "definition_status": "defined"}
    )
    assert mf.definition_status == m.MANIFEST_DEFINITION_DEFINED


def test_definition_status_distinct_from_lifecycle_status() -> None:
    """The new axis must not collide with the pre-existing lifecycle `status`
    (active/paused/...) — both coexist on one manifest."""
    mf = m.ApplicationManifest(
        id="z", name="Z", bot_id="b", status="paused",
        definition_status="defined",
    )
    d = mf.to_dict()
    assert d["status"] == "paused"
    assert d["definition_status"] == "defined"


# ── 2. Born-status mapping ───────────────────────────────────────────

def test_born_status_scanner_discovery_is_discovered() -> None:
    assert m.born_definition_status("discovered") == "discovered"


def test_born_status_empty_and_legacy_aliases_are_discovered() -> None:
    # Empty/missing source = legacy scanner detection → discovered.
    assert m.born_definition_status("") == "discovered"
    assert m.born_definition_status(None) == "discovered"  # type: ignore[arg-type]
    # Legacy aliases normalise: "detected"/"llm-inferred" → discovered.
    assert m.born_definition_status("detected") == "discovered"
    assert m.born_definition_status("llm-inferred") == "discovered"


def test_born_status_authored_sources_are_defined() -> None:
    for src in (
        "user_created", "gallery_installed", "bot_created",
        "rsi_proposed", "file_imported", "forge_built",
    ):
        assert m.born_definition_status(src) == "defined", src
    # Legacy authored aliases too.
    assert m.born_definition_status("user-defined") == "defined"
    assert m.born_definition_status("imported") == "defined"


# ── 3. Migration default ─────────────────────────────────────────────

def _migrate_dict(data: dict) -> dict:
    """Run migrate_manifest over a dict written to a temp file; return result."""
    d = Path(tempfile.mkdtemp())
    p = d / f"{data['id']}.json"
    p.write_text(json.dumps(data))
    m.migrate_manifest(p)
    return json.loads(p.read_text())


def test_migration_lands_existing_manifests_at_discovered() -> None:
    """DELIBERATE §9.1: every PRE-EXISTING manifest migrates to "discovered"
    regardless of source — NO bulk auto-promote. Even a gallery-installed app."""
    for src in ("gallery_installed", "user_created", "discovered", "forge_built"):
        got = _migrate_dict(
            {"id": f"app-{src}", "name": "A", "bot_id": "b",
             "source": src, "schema_version": 24}
        )
        assert got["definition_status"] == "discovered", src


def test_migration_preserves_born_defined() -> None:
    """A born-defined manifest is NOT clobbered by the populate-on-absent
    migration default."""
    got = _migrate_dict(
        {"id": "born-defined", "name": "A", "bot_id": "b",
         "source": "gallery_installed", "definition_status": "defined",
         "schema_version": 24}
    )
    assert got["definition_status"] == "defined"


def test_migration_is_idempotent() -> None:
    got = _migrate_dict(
        {"id": "idem", "name": "A", "bot_id": "b",
         "source": "gallery_installed", "schema_version": 24}
    )
    assert got["definition_status"] == "discovered"
    # second pass over the already-migrated file keeps it.
    d = Path(tempfile.mkdtemp())
    p = d / "idem.json"
    p.write_text(json.dumps(got))
    m.migrate_manifest(p)
    assert json.loads(p.read_text())["definition_status"] == "discovered"


# ── 4. Promote / Demote (legacy shape) ───────────────────────────────

def _discovered_legacy_manifest() -> dict:
    return {
        "id": "x", "name": "X", "bot_id": "b", "description": "does X",
        "definition_status": "discovered",
        "provenance": {"field_origins": {
            "name": {"source": "observational"},
            "description": {"source": "observational"},
        }},
    }


def test_promote_sets_defined_and_marks_anchored_fields_authored() -> None:
    mf = _discovered_legacy_manifest()
    result = promote_to_defined(mf, by="ui:operator")
    assert mf["definition_status"] == "defined"
    assert result["was_already_defined"] is False
    assert set(result["marked_fields"]) == set(ANCHORED_IDENTITY_FIELDS)
    # The anchored-identity fields are now authored → reconciliation surfaces
    # drift as a chip instead of silently overwriting.
    for fld in ANCHORED_IDENTITY_FIELDS:
        assert is_field_authored(mf, fld), fld
    assert mf["provenance"]["last_promoted_at"]
    assert mf["provenance"]["last_promoted_by"] == "ui:operator"


def test_promote_is_idempotent() -> None:
    mf = _discovered_legacy_manifest()
    promote_to_defined(mf)
    second = promote_to_defined(mf)
    assert second["was_already_defined"] is True
    assert second["marked_fields"] == []  # nothing re-stamped
    assert mf["definition_status"] == "defined"


def test_promote_demote_round_trip() -> None:
    mf = _discovered_legacy_manifest()
    promote_to_defined(mf)
    result = demote_to_discovered(mf, by="ui:operator")
    assert mf["definition_status"] == "discovered"
    assert set(result["relaxed_fields"]) == set(ANCHORED_IDENTITY_FIELDS)
    # The promotion's own marks are relaxed back to observational.
    for fld in ANCHORED_IDENTITY_FIELDS:
        assert mf["provenance"]["field_origins"][fld]["source"] == "observational"


def test_demote_is_idempotent() -> None:
    mf = _discovered_legacy_manifest()
    result = demote_to_discovered(mf)
    assert result["was_already_discovered"] is True
    assert result["relaxed_fields"] == []
    assert mf["definition_status"] == "discovered"


def test_demote_preserves_independently_authored_fields() -> None:
    """Demote relaxes ONLY the marks promotion set (source=="confirmed"); a
    field the operator authored another way (user_authored) keeps its
    authorship through a promote/demote round-trip."""
    mf = _discovered_legacy_manifest()
    mf["provenance"]["field_origins"]["name"]["source"] = "user_authored"
    promote_to_defined(mf)
    # name stays user_authored (promote never overwrites an authored field).
    assert mf["provenance"]["field_origins"]["name"]["source"] == "user_authored"
    # description (was observational) became confirmed.
    assert mf["provenance"]["field_origins"]["description"]["source"] == "confirmed"
    demote_to_discovered(mf)
    # name preserved; description relaxed back.
    assert mf["provenance"]["field_origins"]["name"]["source"] == "user_authored"
    assert mf["provenance"]["field_origins"]["description"]["source"] == "observational"


def test_demote_does_not_clobber_confirmed_set_by_another_path() -> None:
    """A field made `confirmed` by a DIFFERENT channel (e.g. the coherence
    "Mark as ready" click also stamps PROVENANCE_CONFIRMED) keeps its vouch
    through a promote/demote round-trip — demote relaxes only its own marks
    (via == "definition_status:promote")."""
    mf = _discovered_legacy_manifest()
    # name was vouched via a different path BEFORE this lifecycle ran.
    mf["provenance"]["field_origins"]["name"] = {
        "source": "confirmed", "via": "mark_ready", "by": "ui:operator",
    }
    promote_to_defined(mf)
    # promote leaves the already-authored name untouched (not re-stamped).
    assert mf["provenance"]["field_origins"]["name"]["via"] == "mark_ready"
    demote_to_discovered(mf)
    # demote must NOT relax the mark_ready-confirmed name.
    assert mf["provenance"]["field_origins"]["name"]["source"] == "confirmed"
    assert mf["provenance"]["field_origins"]["name"]["via"] == "mark_ready"
    # description (this lifecycle's own mark) IS relaxed, with no stale residue.
    desc = mf["provenance"]["field_origins"]["description"]
    assert desc["source"] == "observational"
    assert desc["via"] == "definition_status:demote"


def test_migration_coerces_out_of_enum_definition_status() -> None:
    """A corrupt on-disk value (LLM/hand-edit leak) normalises to the safe
    default rather than lingering non-conformant."""
    got = _migrate_dict(
        {"id": "corrupt", "name": "A", "bot_id": "b",
         "source": "discovered", "definition_status": "v27", "schema_version": 24}
    )
    assert got["definition_status"] == "discovered"


# ── 4b. v7-arc shape guard ───────────────────────────────────────────

def _v7_arc_instance() -> dict:
    return {
        "instance_id": "i-1", "bot_id": "b", "manifest_shape": "v7-arc",
        "definition_status": "discovered",
        "provenance": {
            "spec_id": "p-12345678", "spec_version": "2026.06.01-1.0",
            "installed_at": "2026-06-01T00:00:00Z", "installed_by": "forge_engine",
        },
    }


def test_v7_arc_promote_flips_status_without_touching_install_provenance() -> None:
    """A v7-arc Instance's `provenance` block is INSTALL provenance
    (additionalProperties false, no field_origins). Promote must flip
    definition_status for the existence guarantee WITHOUT corrupting it."""
    inst = _v7_arc_instance()
    result = promote_to_defined(inst)
    assert inst["definition_status"] == "defined"
    assert result["marked_fields"] == []
    assert set(inst["provenance"].keys()) == {
        "spec_id", "spec_version", "installed_at", "installed_by"
    }
    assert "field_origins" not in inst["provenance"]
    assert "last_promoted_at" not in inst["provenance"]


def test_v7_arc_demote_flips_status_without_touching_install_provenance() -> None:
    inst = _v7_arc_instance()
    promote_to_defined(inst)
    demote_to_discovered(inst)
    assert inst["definition_status"] == "discovered"
    assert set(inst["provenance"].keys()) == {
        "spec_id", "spec_version", "installed_at", "installed_by"
    }


def test_v7_arc_pre_shape_also_guarded() -> None:
    """The transient v7-arc-pre Instance carries the same install-provenance
    block — the shape guard must cover it too."""
    inst = _v7_arc_instance()
    inst["manifest_shape"] = "v7-arc-pre"
    result = promote_to_defined(inst)
    assert inst["definition_status"] == "defined"
    assert result["marked_fields"] == []
    assert "field_origins" not in inst["provenance"]
    assert "last_promoted_at" not in inst["provenance"]


# ── 5. Existence guarantee (L3 archival shield) ──────────────────────

_INFRA_SHELL = {
    "id": "liveness", "name": "Liveness Monitoring System", "bot_id": "b",
    "source": "discovered",
    "objective": "Periodic automated gateway liveness pinging and self-healing",
    "evidence_files": [], "realized_files": [], "scheduled_actions": [],
}


def _archive_in_temp(manifest: dict) -> list[str]:
    d = Path(tempfile.mkdtemp())
    (d / f"{manifest['id']}.json").write_text(json.dumps(manifest))
    return _archive_platform_file_only_stubs(d)


def test_is_defined_predicate() -> None:
    assert _is_defined({"definition_status": "defined"}) is True
    assert _is_defined({"definition_status": "discovered"}) is False
    assert _is_defined({}) is False  # absent reads as not-defined


def test_discovered_zero_file_infra_shell_is_archived() -> None:
    """Baseline: a discovered incoherent zero-realization infra shell archives
    as before (Rule 4)."""
    archived = _archive_in_temp({**_INFRA_SHELL, "definition_status": "discovered"})
    assert any("liveness" in a for a in archived)


def test_defined_zero_file_manifest_survives_archival() -> None:
    """EXISTENCE GUARANTEE: the SAME zero-file shell, once defined, is never
    auto-archived."""
    d = Path(tempfile.mkdtemp())
    p = d / "liveness.json"
    p.write_text(json.dumps({**_INFRA_SHELL, "definition_status": "defined"}))
    archived = _archive_platform_file_only_stubs(d)
    assert not any("liveness" in a for a in archived)
    assert p.exists()  # still on disk


def test_defined_shield_is_orthogonal_to_source() -> None:
    """A scanner-discovered app (source="discovered") gains existence
    protection purely by being promoted — protection it never had from
    source alone."""
    promoted = {**_INFRA_SHELL, "source": "discovered", "definition_status": "defined"}
    archived = _archive_in_temp(promoted)
    assert not any("liveness" in a for a in archived)
