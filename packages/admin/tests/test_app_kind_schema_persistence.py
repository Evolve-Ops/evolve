"""Schema + persistence wiring for the v25 app_kind / classification field.

Asserts the inert default reaches both manifest shapes, that migration backfills
it on legacy manifests, and that a v7-arc native mint carries a stamped label
onto the Instance (and survives hydration). Pure-Python; no LLM.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import manifest as m  # noqa: E402
from evolve_admin.applications import native_write as nw  # noqa: E402
from evolve_admin.applications import purpose_classifier as pc  # noqa: E402


def test_dataclass_inert_default():
    inst = m.ApplicationManifest(id="x", name="X", bot_id="personal-bot")
    assert inst.app_kind == pc.APP_KIND_APPLICATION == "application"
    assert inst.classification == {}


def test_schema_version_bumped_for_system_verdict():
    # v25 added app_kind/classification; v26 (Slice 2) widened the enum to add
    # "system" and stamped classifier_version into the classification block.
    assert m.MANIFEST_SCHEMA_VERSION >= 26


def test_migrate_backfills_inert_default(tmp_path: Path):
    p = tmp_path / "legacy-app.json"
    p.write_text(json.dumps({"id": "legacy-app", "name": "Legacy App"}))
    m.migrate_manifest(p)
    data = json.loads(p.read_text())
    assert data["app_kind"] == "application"
    assert data["classification"] == {}


def test_migrate_does_not_clobber_existing_label(tmp_path: Path):
    """A manifest already labeled capability keeps its label through migration."""
    p = tmp_path / "cap.json"
    p.write_text(json.dumps({
        "id": "cap", "name": "Cap",
        "app_kind": "capability",
        "classification": {"kind": "capability", "confidence": 0.9,
                           "classified_by": pc.CLASSIFIED_BY},
    }))
    m.migrate_manifest(p)
    data = json.loads(p.read_text())
    assert data["app_kind"] == "capability"
    assert data["classification"]["kind"] == "capability"


def _shared(tmp_path: Path) -> Path:
    s = tmp_path / "shared"
    (s / "gallery" / "local").mkdir(parents=True, exist_ok=True)
    return s


def test_v7_arc_mint_carries_label_onto_instance(tmp_path: Path):
    shared = _shared(tmp_path)
    caps = tmp_path / "manifests"
    caps.mkdir()
    legacy = {
        "id": "google-services-integration",
        "name": "Google Services Integration",
        "objective": "OAuth-based integration with Google APIs",
        "app_kind": "capability",
        "classification": {"kind": "capability", "confidence": 0.93,
                           "model_tier": "tier2", "classified_by": pc.CLASSIFIED_BY},
    }
    instance_path = caps / "google-services-integration.json"
    result = nw.mint_v7_arc_app(
        legacy, shared_dir=shared, bot_id="personal-bot",
        installed_by="scanner", instance_path=instance_path,
    )
    assert not result.errors, result.errors
    instance = json.loads(instance_path.read_text())
    assert instance["manifest_shape"] == "v7-arc"
    assert instance["app_kind"] == "capability"
    assert instance["classification"]["kind"] == "capability"


def test_v7_arc_unstamped_mint_has_no_label_keys(tmp_path: Path):
    """An un-classified mint (no app_kind/classification on the legacy dict)
    does not gain spurious keys — the passthrough is guarded by ``if value:``.
    The inert default is supplied later by migrate/load, not the mint."""
    shared = _shared(tmp_path)
    caps = tmp_path / "manifests"
    caps.mkdir()
    legacy = {"id": "plain-app", "name": "Plain App", "objective": "Do a goal."}
    instance_path = caps / "plain-app.json"
    nw.mint_v7_arc_app(
        legacy, shared_dir=shared, bot_id="personal-bot",
        installed_by="scanner", instance_path=instance_path,
    )
    instance = json.loads(instance_path.read_text())
    # No empty-dict / empty-string noise written onto the instance.
    assert "classification" not in instance
    assert instance.get("app_kind", "") in ("", None) or "app_kind" not in instance


def test_classified_instance_validates_against_v7_schema(tmp_path: Path):
    """A v7-arc Instance carrying app_kind + classification still validates
    against the (additionalProperties:false) instance JSON schema — the schema
    was extended in the same bite."""
    import jsonschema

    schema_dir = Path(__file__).resolve().parents[3] / "docs" / "schemas"
    schema = json.loads((schema_dir / "manifest-v7-instance.schema.json").read_text())
    shared = _shared(tmp_path)
    caps = tmp_path / "manifests"
    caps.mkdir()
    legacy = {
        "id": "google-services-integration",
        "name": "Google Services Integration",
        "objective": "OAuth-based integration with Google APIs",
        "app_kind": "capability",
        "classification": {
            "kind": "capability", "confidence": 0.93, "rationale": "bare capability",
            "model_tier": "tier2", "classified_by": pc.CLASSIFIED_BY,
            "raw_kind": "capability",
        },
    }
    instance_path = caps / "google-services-integration.json"
    nw.mint_v7_arc_app(
        legacy, shared_dir=shared, bot_id="personal-bot",
        installed_by="scanner", instance_path=instance_path,
    )
    instance = json.loads(instance_path.read_text())
    assert instance["app_kind"] == "capability"
    errors = [e.message for e in jsonschema.Draft7Validator(schema).iter_errors(instance)]
    assert errors == [], errors


def test_hydration_preserves_instance_label(tmp_path: Path):
    shared = _shared(tmp_path)
    caps = tmp_path / "manifests"
    caps.mkdir()
    legacy = {
        "id": "google-services-integration",
        "name": "Google Services Integration",
        "objective": "OAuth-based integration with Google APIs",
        "app_kind": "capability",
        "classification": {"kind": "capability", "confidence": 0.93,
                           "classified_by": pc.CLASSIFIED_BY},
    }
    instance_path = caps / "google-services-integration.json"
    nw.mint_v7_arc_app(
        legacy, shared_dir=shared, bot_id="personal-bot",
        installed_by="scanner", instance_path=instance_path,
    )
    instance = json.loads(instance_path.read_text())
    hydrated = m.hydrate_v7_arc_instance(instance, shared)
    assert hydrated["app_kind"] == "capability"


def test_system_instance_with_version_validates_against_v7_schema(tmp_path: Path):
    """Slice 2: a v7-arc Instance labeled app_kind='system' and carrying the new
    classifier_version field validates against the (additionalProperties:false)
    instance JSON schema — the enum and the new property were added together."""
    import jsonschema

    schema_dir = Path(__file__).resolve().parents[3] / "docs" / "schemas"
    schema = json.loads((schema_dir / "manifest-v7-instance.schema.json").read_text())
    shared = _shared(tmp_path)
    caps = tmp_path / "manifests"
    caps.mkdir()
    legacy = {
        "id": "operations-automation",
        "name": "Operations Automation",
        "objective": "Self-healing infrastructure management for the pod.",
        "app_kind": "system",
        "classification": {
            "kind": "system", "confidence": 0.95, "rationale": "operates the runtime",
            "model_tier": "tier2", "classified_by": pc.CLASSIFIED_BY,
            "classifier_version": pc.CLASSIFIER_VERSION,
        },
    }
    instance_path = caps / "operations-automation.json"
    nw.mint_v7_arc_app(
        legacy, shared_dir=shared, bot_id="personal-bot",
        installed_by="scanner", instance_path=instance_path,
    )
    instance = json.loads(instance_path.read_text())
    assert instance["app_kind"] == "system"
    assert instance["classification"]["classifier_version"] == pc.CLASSIFIER_VERSION
    errors = [e.message for e in jsonschema.Draft7Validator(schema).iter_errors(instance)]
    assert errors == [], errors
