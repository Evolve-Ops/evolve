"""Schema v24 — privacy{} + audience_scoping{} on the legacy manifest shape.

Pins the three load-bearing behaviors of the field add (manifest-v7
Slice 2, docs/spec-manifest-v7-slicing-2026-06-10.md §4):

  1. from_dict/to_dict round-trip the blocks (without dataclass fields,
     from_dict's unknown-key filter silently drops them on every load —
     the v20/v21 lesson).
  2. migrate_manifest adds EMPTY defaults — absence means "not yet
     declared"; the migration must not invent a trust boundary.
  3. The schema_version stamp moves to the current constant.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.manifest import (  # noqa: E402
    MANIFEST_SCHEMA_VERSION,
    ApplicationManifest,
    migrate_manifest,
)


def _declared_blocks() -> tuple[dict, dict]:
    privacy = {
        "user_data_collected": ["intake_log"],
        "opt_out_command": "/x opt-out",
        "consent_notice": "I log intake events.",
        "retention_days": 90,
        "shareable_in_lessons": True,
    }
    scoping = {
        "operator": "named_users",
        "approved_surfaces": ["telegram_dm"],
        "role_capabilities": {"member": ["read"]},
        "operator_bypasses": [],
    }
    return privacy, scoping


class TestRoundTrip:
    def test_from_dict_keeps_declared_blocks(self):
        privacy, scoping = _declared_blocks()
        m = ApplicationManifest.from_dict({
            "id": "x", "name": "X", "bot_id": "team_bot_a",
            "privacy": privacy,
            "audience_scoping": scoping,
        })
        assert m.privacy == privacy
        assert m.audience_scoping == scoping

    def test_to_dict_emits_blocks(self):
        privacy, scoping = _declared_blocks()
        m = ApplicationManifest(
            id="x", name="X", bot_id="team_bot_a",
            privacy=privacy, audience_scoping=scoping,
        )
        d = m.to_dict()
        assert d["privacy"] == privacy
        assert d["audience_scoping"] == scoping
        # Full round-trip through JSON (the save_manifest path).
        again = ApplicationManifest.from_dict(json.loads(json.dumps(d)))
        assert again.privacy == privacy
        assert again.audience_scoping == scoping

    def test_defaults_are_inert_empty_dicts(self):
        m = ApplicationManifest(id="x", name="X", bot_id="team_bot_a")
        assert m.privacy == {}
        assert m.audience_scoping == {}


class TestMigration:
    def _migrate(self, tmp_path: Path, data: dict) -> dict:
        p = tmp_path / "app.json"
        p.write_text(json.dumps(data))
        migrate_manifest(p)
        return json.loads(p.read_text())

    def test_adds_empty_blocks_to_pre_v24_manifest(self, tmp_path):
        out = self._migrate(tmp_path, {
            "id": "x", "name": "X", "bot_id": "team_bot_a",
            "schema_version": 23,
        })
        assert out["privacy"] == {}
        assert out["audience_scoping"] == {}
        assert out["schema_version"] == MANIFEST_SCHEMA_VERSION

    def test_never_overwrites_declared_blocks(self, tmp_path):
        privacy, scoping = _declared_blocks()
        out = self._migrate(tmp_path, {
            "id": "x", "name": "X", "bot_id": "team_bot_a",
            "privacy": privacy,
            "audience_scoping": scoping,
        })
        assert out["privacy"] == privacy
        assert out["audience_scoping"] == scoping

    def test_constant_is_at_least_24(self):
        # v23 was taken by delivery_contract (#2642); Slice 2 is v24. If a
        # parallel session takes 24 first, the field-block label and this
        # floor move together with the constant.
        assert MANIFEST_SCHEMA_VERSION >= 24


class TestV7ArcHydration:
    def test_hydrate_overlays_spec_blocks(self, tmp_path):
        # privacy/audience_scoping are Spec-owned facts — the hydrated
        # Instance view (what list_manifests feeds the audit projection)
        # must carry the Spec's declared blocks.
        from evolve_admin.applications.manifest import hydrate_v7_arc_instance

        privacy, scoping = _declared_blocks()
        spec_dir = tmp_path / "gallery" / "local" / "p-aaaa1111"
        spec_dir.mkdir(parents=True)
        (spec_dir / "2026.05.20-1.0.json").write_text(json.dumps({
            "spec_id": "p-aaaa1111",
            "spec_version": "2026.05.20-1.0",
            "name": "Journal",
            "privacy": privacy,
            "audience_scoping": scoping,
        }))
        instance = {
            "instance_id": "i-12345678",
            "manifest_shape": "v7-arc",
            "status": "active",
            "provenance": {
                "spec_id": "p-aaaa1111",
                "spec_version": "2026.05.20-1.0",
            },
        }
        hydrated = hydrate_v7_arc_instance(instance, tmp_path)
        assert hydrated["privacy"] == privacy
        assert hydrated["audience_scoping"] == scoping

    def test_hydrate_keeps_instance_local_blocks(self, tmp_path):
        from evolve_admin.applications.manifest import hydrate_v7_arc_instance

        privacy, _ = _declared_blocks()
        spec_dir = tmp_path / "gallery" / "local" / "p-aaaa1111"
        spec_dir.mkdir(parents=True)
        (spec_dir / "2026.05.20-1.0.json").write_text(json.dumps({
            "spec_id": "p-aaaa1111",
            "spec_version": "2026.05.20-1.0",
            "name": "Journal",
            "privacy": privacy,
        }))
        local_privacy = {"user_data_collected": ["instance_override"]}
        instance = {
            "instance_id": "i-12345678",
            "manifest_shape": "v7-arc",
            "privacy": local_privacy,
            "provenance": {
                "spec_id": "p-aaaa1111",
                "spec_version": "2026.05.20-1.0",
            },
        }
        hydrated = hydrate_v7_arc_instance(instance, tmp_path)
        assert hydrated["privacy"] == local_privacy
