"""Tests for privacy_scoping_validator — the manifest-v7 Slice 2 gate.

Spec: internal/spec-manifest-v7-slicing-2026-06-10.md §4.1. Pinned structure
mirrors docs/schemas/manifest-v7-spec.schema.json; vocabulary stays open
(owned by the autonomy-ladder track).
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.manifest import ApplicationManifest  # noqa: E402
from evolve_admin.applications.privacy_scoping_validator import (  # noqa: E402
    GROUP_CHANNELS,
    default_audience_scoping_block,
    default_privacy_block,
    project_data_boundary,
    validate_privacy_scoping,
)


def _trigger(**overrides) -> dict:
    base = {
        "id": "t1",
        "source": "telegram",
        "match": {"channel": "telegram_dm", "pattern": "^/x"},
        "audience": "operator_only",
        "invokes": "handler",
    }
    base.update(overrides)
    return base


# ── Absence = "not yet declared" — no flag day ──────────────────────────────


class TestAbsenceIsAllowed:
    def test_empty_manifest_passes(self):
        result = validate_privacy_scoping({})
        assert result["ok"] is True
        assert result["severity"] == "info"
        assert result["privacy_declared"] is False
        assert result["audience_declared"] is False

    def test_empty_dict_blocks_count_as_not_declared(self):
        # The inert dataclass default on pre-v24 manifests is {}.
        result = validate_privacy_scoping({"privacy": {}, "audience_scoping": {}})
        assert result["ok"] is True
        assert result["privacy_declared"] is False
        assert result["audience_declared"] is False

    def test_manifest_object_input(self):
        manifest = ApplicationManifest(id="x", name="X", bot_id="team_bot_a")
        result = validate_privacy_scoping(manifest)
        assert result["ok"] is True


# ── Canonical defaults must validate clean ──────────────────────────────────


class TestDefaultsAreSelfConsistent:
    def test_default_blocks_pass_validation(self):
        result = validate_privacy_scoping({
            "privacy": default_privacy_block(),
            "audience_scoping": default_audience_scoping_block(),
        })
        assert result["ok"] is True, result["errors"]
        assert result["privacy_declared"] is True
        assert result["audience_declared"] is True

    def test_default_audience_covers_default_trigger_audience(self):
        # The migration's inferred trigger audience ("operator_only") must
        # name a role in the inferred scoping block, or the backfill would
        # warn on its own output.
        block = default_audience_scoping_block()
        assert "operator_only" in block["role_capabilities"]


# ── Pinned structure: privacy ───────────────────────────────────────────────


class TestPrivacyStructure:
    def test_unknown_key_refused(self):
        result = validate_privacy_scoping({
            "privacy": {**default_privacy_block(), "data_sold_to": "nobody"},
        })
        assert result["ok"] is False
        assert result["severity"] == "build_blocker"
        assert any("unknown key" in e for e in result["errors"])

    def test_non_dict_refused(self):
        result = validate_privacy_scoping({"privacy": "we collect nothing"})
        assert result["ok"] is False

    def test_user_data_collected_must_be_string_list(self):
        result = validate_privacy_scoping({
            "privacy": {"user_data_collected": "intake_log"},
        })
        assert result["ok"] is False
        assert any("user_data_collected" in e for e in result["errors"])

    def test_retention_days_bounds_and_types(self):
        for bad in (0, -3, "365", True, 1.5):
            result = validate_privacy_scoping({"privacy": {"retention_days": bad}})
            assert result["ok"] is False, f"retention_days={bad!r} should refuse"
        ok = validate_privacy_scoping({"privacy": {"retention_days": 365}})
        assert ok["ok"] is True

    def test_shareable_in_lessons_must_be_bool(self):
        result = validate_privacy_scoping({
            "privacy": {"shareable_in_lessons": "false"},
        })
        assert result["ok"] is False

    def test_consent_and_opt_out_must_be_strings(self):
        result = validate_privacy_scoping({
            "privacy": {"consent_notice": 42, "opt_out_command": ["/optout"]},
        })
        assert result["ok"] is False
        assert len(result["errors"]) == 2


# ── Pinned structure: audience_scoping ──────────────────────────────────────


class TestAudienceScopingStructure:
    def test_required_trio_enforced(self):
        result = validate_privacy_scoping({
            "audience_scoping": {"operator": "operator_only"},
        })
        assert result["ok"] is False
        assert any("missing required key" in e for e in result["errors"])

    def test_operator_enum(self):
        block = default_audience_scoping_block()
        block["operator"] = "everyone"
        result = validate_privacy_scoping({"audience_scoping": block})
        assert result["ok"] is False
        assert any("operator" in e for e in result["errors"])

    def test_unknown_key_refused(self):
        block = default_audience_scoping_block()
        block["surfaces"] = ["telegram_dm"]
        result = validate_privacy_scoping({"audience_scoping": block})
        assert result["ok"] is False
        assert any("unknown key" in e for e in result["errors"])

    def test_role_capabilities_shape(self):
        block = default_audience_scoping_block()
        block["role_capabilities"] = {"operator_only": "read"}
        result = validate_privacy_scoping({"audience_scoping": block})
        assert result["ok"] is False

    def test_operator_bypasses_optional_but_typed(self):
        block = default_audience_scoping_block()
        del block["operator_bypasses"]
        assert validate_privacy_scoping({"audience_scoping": block})["ok"] is True
        block["operator_bypasses"] = "admin_override"
        assert validate_privacy_scoping({"audience_scoping": block})["ok"] is False

    def test_open_vocabulary_not_enforced(self):
        # Role names, surface ids, bypass ids are open in v1 — novel values
        # must pass (the autonomy ladder owns the vocabulary).
        result = validate_privacy_scoping({
            "audience_scoping": {
                "operator": "named_users",
                "approved_surfaces": ["carrier_pigeon_dm"],
                "role_capabilities": {"ranch_member": ["log_feed"]},
                "operator_bypasses": ["midnight_override"],
            },
        })
        assert result["ok"] is True, result["errors"]


# ── Trigger-audience conformance (the Slice-1 free-string gap) ──────────────


class TestTriggerAudienceConformance:
    def test_conforming_audience_passes(self):
        result = validate_privacy_scoping({
            "audience_scoping": default_audience_scoping_block(),
            "event_triggers": [_trigger(audience="operator_only")],
        })
        assert result["ok"] is True, result["errors"]

    def test_nonconforming_audience_blocks_when_scoping_declared(self):
        result = validate_privacy_scoping({
            "audience_scoping": default_audience_scoping_block(),
            "event_triggers": [_trigger(audience="anyone")],
        })
        assert result["ok"] is False
        assert result["severity"] == "build_blocker"
        assert any("'anyone'" in e and "role_capabilities" in e for e in result["errors"])

    def test_free_string_audience_is_info_when_scoping_absent(self):
        # No flag day: pre-v24 manifests with trigger audiences keep
        # installing; the validator only nudges.
        result = validate_privacy_scoping({
            "event_triggers": [_trigger(audience="anyone")],
        })
        assert result["ok"] is True
        assert result["severity"] == "info"
        assert "unpinned" in result["message"]

    def test_missing_audience_is_fine(self):
        trigger = _trigger()
        del trigger["audience"]
        result = validate_privacy_scoping({
            "audience_scoping": default_audience_scoping_block(),
            "event_triggers": [trigger],
        })
        assert result["ok"] is True


# ── Group-surface consent gate ──────────────────────────────────────────────


class TestGroupSurfaceConsent:
    def test_group_trigger_without_consent_blocks(self):
        for channel in sorted(GROUP_CHANNELS):
            result = validate_privacy_scoping({
                "event_triggers": [
                    _trigger(match={"channel": channel, "pattern": "^/x"},
                             audience=""),
                ],
            })
            assert result["ok"] is False, f"channel={channel} should require consent"
            assert any("consent_notice" in e for e in result["errors"])

    def test_group_trigger_with_consent_passes(self):
        privacy = default_privacy_block()
        privacy["consent_notice"] = "I watch for /track messages here."
        result = validate_privacy_scoping({
            "privacy": privacy,
            "event_triggers": [
                _trigger(match={"channel": "slack_channel", "pattern": "^/track"},
                         audience=""),
            ],
        })
        assert result["ok"] is True, result["errors"]
        assert result["group_trigger_count"] == 1

    def test_dm_trigger_needs_no_consent(self):
        result = validate_privacy_scoping({
            "event_triggers": [
                _trigger(match={"channel": "telegram_dm", "pattern": "^/x"},
                         audience=""),
            ],
        })
        assert result["ok"] is True
        assert result["group_trigger_count"] == 0

    def test_whitespace_consent_does_not_satisfy(self):
        result = validate_privacy_scoping({
            "privacy": {**default_privacy_block(), "consent_notice": "   "},
            "event_triggers": [
                _trigger(match={"channel": "any", "pattern": "."}, audience=""),
            ],
        })
        assert result["ok"] is False


# ── Data-boundary projection (audit+score surface) ──────────────────────────


class TestProjectDataBoundary:
    def test_undeclared_app(self):
        out = project_data_boundary({"id": "journal", "name": "Journal"})
        assert out["app_id"] == "journal"
        assert out["privacy_declared"] is False
        assert out["audience_declared"] is False
        assert out["collects"] == []
        assert out["retention_days"] is None
        assert out["shareable_in_lessons"] is False

    def test_declared_app_projects_structured_fields(self):
        out = project_data_boundary({
            "id": "protein",
            "display_name": "Protein Tracker",
            "status": "paused",
            "privacy": {
                "user_data_collected": ["intake_log", "timestamps"],
                "opt_out_command": "/protein opt-out",
                "consent_notice": "I log your protein intake.",
                "retention_days": 365,
                "shareable_in_lessons": False,
            },
            "audience_scoping": {
                "operator": "operator_only",
                "approved_surfaces": ["telegram_dm"],
                "role_capabilities": {"operator_only": ["read", "write"]},
                "operator_bypasses": ["admin_override"],
            },
            "event_triggers": [
                {"id": "t1", "match": {"channel": "telegram_group", "pattern": "x"}},
                {"id": "t2", "match": {"channel": "telegram_dm", "pattern": "y"}},
            ],
        })
        assert out["name"] == "Protein Tracker"
        assert out["status"] == "paused"
        assert out["collects"] == ["intake_log", "timestamps"]
        assert out["opt_out_command"] == "/protein opt-out"
        assert out["retention_days"] == 365
        assert out["operator"] == "operator_only"
        assert out["approved_surfaces"] == ["telegram_dm"]
        assert out["roles"] == ["operator_only"]
        assert out["trigger_count"] == 2
        assert out["group_trigger_count"] == 1

    def test_malformed_blocks_degrade_to_empty(self):
        out = project_data_boundary({
            "id": "x",
            "privacy": "prose",
            "audience_scoping": ["operator_only"],
        })
        assert out["privacy_declared"] is False
        assert out["audience_declared"] is False
        assert out["collects"] == []
