"""Schema v29 — provided_capabilities[] on the manifest.

The app-declared half of the Layer-2 per-role tool-loading model
(internal/spec-user-roster-and-roles-2026-06-07.md §4/§8;
internal/design-layer2-tool-loading-filter-2026-07-16.md). This slice lands the
schema field + validation ONLY — the enforcement gate that unions
requires_mcp_tools per resolved role is a separate later build step.

Pins the load-bearing behaviors of the field add:

  1. from_dict/to_dict round-trip the entries with requires_mcp_tools +
     default_role_binding preserved (without the dataclass field,
     from_dict's unknown-key filter silently drops them on every load —
     the v20/v21/v24 lesson).
  2. migrate_manifest adds an EMPTY default — absence means "not yet
     declared"; the migration must not invent capability declarations.
  3. A manifest WITHOUT the field still loads (inert default) — back-compat.
  4. validate_provided_capabilities rejects a bad default_role_binding
     (unknown role) and a non-string requires_mcp_tools entry, and accepts
     a well-formed block.
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
    validate_provided_capabilities,
)


def _declared_caps() -> list[dict]:
    return [
        {
            "name": "app.archive.add",
            "description": "Add an article URL to the archive",
            "requires_mcp_tools": ["archive.add"],
            "default_role_binding": "participant",
        },
        {
            "name": "app.archive.delete",
            "description": "Remove an article from the archive",
            "requires_mcp_tools": ["archive.delete"],
            "default_role_binding": "primary_user",
        },
    ]


class TestRoundTrip:
    def test_from_dict_keeps_declared_capabilities(self):
        caps = _declared_caps()
        m = ApplicationManifest.from_dict({
            "id": "x", "name": "X", "bot_id": "team_bot_a",
            "provided_capabilities": caps,
        })
        assert m.provided_capabilities == caps

    def test_to_dict_round_trips(self):
        caps = _declared_caps()
        m = ApplicationManifest(
            id="x", name="X", bot_id="team_bot_a",
            provided_capabilities=caps,
        )
        data = m.to_dict()
        assert data["provided_capabilities"] == caps
        # Full round-trip through serialize → from_dict preserves values.
        m2 = ApplicationManifest.from_dict(json.loads(json.dumps(data)))
        assert m2.provided_capabilities == caps
        assert m2.provided_capabilities[0]["requires_mcp_tools"] == ["archive.add"]
        assert m2.provided_capabilities[1]["default_role_binding"] == "primary_user"


class TestDefaults:
    def test_dataclass_default_is_empty_list(self):
        m = ApplicationManifest(id="x", name="X", bot_id="team_bot_a")
        assert m.provided_capabilities == []

    def test_manifest_without_field_still_loads(self):
        # Back-compat: a manifest that predates v29 loads with the inert
        # default rather than raising.
        m = ApplicationManifest.from_dict({
            "id": "x", "name": "X", "bot_id": "team_bot_a",
        })
        assert m.provided_capabilities == []

    def test_migrate_adds_empty_default(self, tmp_path):
        path = tmp_path / "legacy.json"
        path.write_text(json.dumps({
            "id": "x", "name": "X", "bot_id": "team_bot_a",
        }))
        migrate_manifest(path)
        data = json.loads(path.read_text())
        assert data["provided_capabilities"] == []
        assert data["schema_version"] == MANIFEST_SCHEMA_VERSION

    def test_migrate_preserves_declared_capabilities(self, tmp_path):
        caps = _declared_caps()
        path = tmp_path / "declared.json"
        path.write_text(json.dumps({
            "id": "x", "name": "X", "bot_id": "team_bot_a",
            "provided_capabilities": caps,
        }))
        migrate_manifest(path)
        data = json.loads(path.read_text())
        assert data["provided_capabilities"] == caps


class TestValidation:
    def test_wellformed_block_passes(self):
        assert validate_provided_capabilities(_declared_caps()) == []

    def test_empty_list_passes(self):
        assert validate_provided_capabilities([]) == []

    def test_null_default_role_binding_passes(self):
        errors = validate_provided_capabilities([
            {"name": "app.x", "requires_mcp_tools": ["x.tool"],
             "default_role_binding": None},
        ])
        assert errors == []

    def test_missing_default_role_binding_passes(self):
        errors = validate_provided_capabilities([
            {"name": "app.x", "requires_mcp_tools": ["x.tool"]},
        ])
        assert errors == []

    def test_unknown_role_rejected(self):
        errors = validate_provided_capabilities([
            {"name": "app.x", "requires_mcp_tools": ["x.tool"],
             "default_role_binding": "wizard"},
        ])
        assert errors
        assert any("default_role_binding" in e and "wizard" in e for e in errors)

    def test_non_string_requires_mcp_tools_entry_rejected(self):
        errors = validate_provided_capabilities([
            {"name": "app.x", "requires_mcp_tools": ["ok", 42]},
        ])
        assert errors
        assert any("requires_mcp_tools" in e for e in errors)

    def test_empty_string_requires_mcp_tools_entry_rejected(self):
        errors = validate_provided_capabilities([
            {"name": "app.x", "requires_mcp_tools": ["ok", "  "]},
        ])
        assert errors
        assert any("requires_mcp_tools" in e for e in errors)

    def test_missing_name_rejected(self):
        errors = validate_provided_capabilities([
            {"requires_mcp_tools": ["x.tool"]},
        ])
        assert errors
        assert any("name" in e for e in errors)

    def test_unknown_key_rejected(self):
        errors = validate_provided_capabilities([
            {"name": "app.x", "bogus": True},
        ])
        assert errors
        assert any("bogus" in e for e in errors)

    def test_duplicate_name_rejected(self):
        errors = validate_provided_capabilities([
            {"name": "app.x", "requires_mcp_tools": ["a"]},
            {"name": "app.x", "requires_mcp_tools": ["b"]},
        ])
        assert errors
        assert any("more than once" in e for e in errors)

    def test_non_list_rejected(self):
        errors = validate_provided_capabilities({"name": "app.x"})
        assert errors
        assert any("must be a list" in e for e in errors)

    def test_all_known_roles_accepted(self):
        for role in ("admin", "primary_user", "participant", "blocked"):
            errors = validate_provided_capabilities([
                {"name": "app.x", "requires_mcp_tools": ["x"],
                 "default_role_binding": role},
            ])
            assert errors == [], f"role {role!r} should be accepted"
