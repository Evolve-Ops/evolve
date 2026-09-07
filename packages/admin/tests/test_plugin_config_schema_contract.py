"""Contract test: plugin config schema consistency across Python, JSON, and TypeScript.

The plugin config shape is declared in three independent places:

1. deploy.py::_PLUGIN_CONFIG_DEFAULTS  — Python dict (deploy-time defaults)
2. openclaw.plugin.json::configSchema  — JSON Schema (runtime validator)
3. config.ts::EvolveConfig             — TypeScript interface (plugin runtime)

This test asserts they agree on shape, catching drift before it ships.
See finding 2026-04-26-101 and PR #336 for the bug class this prevents.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_REPO_ROOT = _ADMIN_DIR.parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.deploy import _PLUGIN_CONFIG_DEFAULTS  # noqa: E402

# ── Helpers ─────────────────────────────────────────────────────────────────

def _load_json_schema_fields() -> dict[str, dict]:
    """Return {field_name: property_def} from openclaw.plugin.json configSchema."""
    plugin_json = _REPO_ROOT / "packages" / "plugin" / "openclaw.plugin.json"
    schema = json.loads(plugin_json.read_text())["configSchema"]
    return schema["properties"]


def _load_json_schema_required() -> set[str]:
    """Return the set of required fields from the JSON schema."""
    plugin_json = _REPO_ROOT / "packages" / "plugin" / "openclaw.plugin.json"
    schema = json.loads(plugin_json.read_text())["configSchema"]
    return set(schema.get("required", []))


def _parse_ts_interface_fields() -> dict[str, str]:
    """Parse EvolveConfig interface fields from config.ts.

    Returns {field_name: ts_type_string}. Handles optional fields (name?).
    Uses simple regex — good enough for the flat interface we have.
    """
    config_ts = _REPO_ROOT / "packages" / "plugin" / "src" / "config.ts"
    source = config_ts.read_text()

    # Extract the EvolveConfig interface body
    match = re.search(
        r"export\s+interface\s+EvolveConfig\s*\{(.*?)\n\}",
        source,
        re.DOTALL,
    )
    assert match, "Could not find EvolveConfig interface in config.ts"
    body = match.group(1)

    fields: dict[str, str] = {}
    for line in body.splitlines():
        line = line.strip()
        # Skip comments and blank lines
        if not line or line.startswith("//") or line.startswith("*"):
            continue
        # Match field declarations: fieldName?: type; or fieldName: type;
        m = re.match(r"(\w+)\??\s*:\s*(.+?)\s*;", line)
        if m:
            fields[m.group(1)] = m.group(2).strip()

    return fields


# ── Identity / role-conditional fields ──────────────────────────────────────
# These are set per-bot by ensure_plugin_config(), not by _PLUGIN_CONFIG_DEFAULTS.
# They're expected to be in the JSON schema and TS interface but NOT in defaults.
# repoRoot is the deploy checkout, injected per-bot from the active platform
# profile's deploy_checkout_default (see openclaw_materializer._resolve_identity).
_IDENTITY_FIELDS = {"botId", "role", "networkId", "sharedDir", "repoRoot", "dashboardEnabled"}

# Fields that exist only in the TS interface (resolved programmatically from
# network.json or with runtime defaults in resolveConfig, not user-configurable
# via the JSON schema). These are the known drift — documenting them here makes
# the test pass today while clearly flagging what Option A should consolidate.
_TS_ONLY_FIELDS = {
    "defaultModel",
    "enableLLMSummarization",
    "classifierHints",
    "applicationPatterns",
    # Resolved from `tier` at runtime by resolveConfig (TIERS table); never
    # written to openclaw.json or read from it. TS interface only.
    "capabilities",
    # Derived at runtime by resolveLayer2Enforce (config.ts): a LOUD warning
    # string emitted when `layer2.enforce` / `layer2Enforce` is present but not
    # a strict boolean (so the gate stays observe-only). Never written to or
    # read from openclaw.json — the operator-facing knob is `layer2Enforce`,
    # which IS in the JSON schema. TS interface only.
    "layer2EnforceWarning",
    # Same shape as layer2EnforceWarning: derived at runtime by resolveConfig
    # when `execFailureAbsorb` is present but not a strict boolean (the
    # absorber stays observe-only and logs this loudly). Never written to or
    # read from openclaw.json — the operator-facing knob is
    # `execFailureAbsorb`, which IS in the JSON schema. TS interface only.
    "execFailureAbsorbWarning",
}


# ── Tests ───────────────────────────────────────────────────────────────────

class TestPluginConfigSchemaContract:
    """Every configurable default in Python must exist in the JSON schema,
    and vice versa. The TS interface must be a superset of both."""

    def test_python_defaults_subset_of_json_schema(self):
        """Every key in _PLUGIN_CONFIG_DEFAULTS must appear in the JSON schema."""
        schema_fields = set(_load_json_schema_fields().keys())
        defaults_fields = set(_PLUGIN_CONFIG_DEFAULTS.keys())

        missing = defaults_fields - schema_fields
        assert not missing, (
            f"Python defaults have fields not in JSON schema: {missing}. "
            f"Add them to openclaw.plugin.json configSchema.properties."
        )

    def test_json_schema_configurable_fields_have_defaults(self):
        """Every non-identity JSON schema field should have a Python default
        or be explicitly identity/role-conditional."""
        schema_fields = set(_load_json_schema_fields().keys())
        defaults_fields = set(_PLUGIN_CONFIG_DEFAULTS.keys())

        configurable = schema_fields - _IDENTITY_FIELDS
        missing = configurable - defaults_fields
        assert not missing, (
            f"JSON schema fields without Python defaults: {missing}. "
            f"Add defaults to _PLUGIN_CONFIG_DEFAULTS in deploy.py, "
            f"or add them to _IDENTITY_FIELDS if they're set per-bot."
        )

    def test_ts_interface_superset_of_json_schema(self):
        """Every JSON schema field must appear in the TypeScript EvolveConfig."""
        schema_fields = set(_load_json_schema_fields().keys())
        ts_fields = set(_parse_ts_interface_fields().keys())

        missing = schema_fields - ts_fields
        assert not missing, (
            f"JSON schema fields missing from TypeScript EvolveConfig: {missing}. "
            f"Add them to EvolveConfig in config.ts."
        )

    def test_ts_interface_superset_of_python_defaults(self):
        """Every Python default field must appear in the TypeScript EvolveConfig."""
        defaults_fields = set(_PLUGIN_CONFIG_DEFAULTS.keys())
        ts_fields = set(_parse_ts_interface_fields().keys())

        missing = defaults_fields - ts_fields
        assert not missing, (
            f"Python default fields missing from TypeScript EvolveConfig: {missing}. "
            f"Add them to EvolveConfig in config.ts."
        )

    def test_ts_only_fields_are_documented(self):
        """Fields in TS but not in JSON schema must be in the _TS_ONLY_FIELDS set.
        If a new field appears in TS without a schema entry, this test forces
        you to either add it to the schema or explicitly acknowledge the gap."""
        schema_fields = set(_load_json_schema_fields().keys())
        ts_fields = set(_parse_ts_interface_fields().keys())

        ts_extra = ts_fields - schema_fields - _IDENTITY_FIELDS
        # Remove fields that are in _IDENTITY_FIELDS (already in schema)
        undocumented = ts_extra - _TS_ONLY_FIELDS
        assert not undocumented, (
            f"TypeScript EvolveConfig has fields not in JSON schema and not in "
            f"_TS_ONLY_FIELDS: {undocumented}. Either add them to the JSON schema "
            f"or add them to _TS_ONLY_FIELDS with a comment explaining why."
        )

    def test_python_default_types_match_json_schema(self):
        """Python default values must be type-compatible with the JSON schema."""
        schema_props = _load_json_schema_fields()
        type_map = {"string": str, "number": (int, float), "boolean": bool}

        for key, value in _PLUGIN_CONFIG_DEFAULTS.items():
            if key not in schema_props:
                continue  # caught by test_python_defaults_subset_of_json_schema
            expected_type = schema_props[key].get("type")
            if expected_type and expected_type in type_map:
                assert isinstance(value, type_map[expected_type]), (
                    f"Default for '{key}' is {type(value).__name__} "
                    f"but JSON schema expects {expected_type}"
                )

    def test_json_schema_required_fields_are_identity(self):
        """Required fields in the JSON schema should be identity fields
        (set per-bot), not configurable defaults."""
        required = _load_json_schema_required()
        defaults_fields = set(_PLUGIN_CONFIG_DEFAULTS.keys())

        overlap = required & defaults_fields
        assert not overlap, (
            f"Fields are both 'required' in JSON schema and have Python defaults: "
            f"{overlap}. Required fields should be identity fields set per-bot, "
            f"not defaults."
        )

    def test_every_default_has_a_tunable_key(self):
        """Every _PLUGIN_CONFIG_DEFAULTS key must have a config_sandbox
        TunableKey targeting plugins.entries.evolve.config.<key>.

        The materializer's drift auto-promote ("redeploys never silently
        destroy edits") only walks the tunable map; a defaults-registry field
        WITHOUT a TunableKey is hard-reset to its default by Pass 3b on every
        deploy. That silently reverted a hand-armed prefixHashLedgerEnabled on
        2026-07-31, and would have silently DISARMED a hand-armed
        layer2Enforce. Add a TunableKey in config_sandbox/schema.py for any
        new defaults-registry field.
        """
        from evolve_admin.openclaw_materializer import _build_field_to_schema_path

        mapped = set(_build_field_to_schema_path())
        missing = set(_PLUGIN_CONFIG_DEFAULTS) - mapped
        assert not missing, (
            f"_PLUGIN_CONFIG_DEFAULTS fields with no config_sandbox TunableKey "
            f"(materializer will reset operator edits to default on every "
            f"deploy): {sorted(missing)}"
        )
