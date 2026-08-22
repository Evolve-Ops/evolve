"""Tests for plugin_intercept_migration.migrate_to_plugin_intercept.

The "Make reliable" backend (web/routes_applications_reliability.py) leans on
this pure synthesizer: an agent_invokes manifest is migrated to
plugin_intercept by giving each event_trigger a valid `invocation` contract,
or refused with a per-trigger reason when one can't be built. The migrator is
conservative — it never produces a manifest that would fail the install gate
(bot_guidance_freelance_validator) — and idempotent.

Spec: docs/spec-agent-freelance-bypass-phase2-2026-06-06.md.
"""

from __future__ import annotations

import copy

from evolve_admin.applications.plugin_intercept_migration import (
    migrate_to_plugin_intercept,
)
from evolve_admin.applications.bot_guidance_freelance_validator import (
    validate_bot_guidance,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _agent_invokes_manifest() -> dict:
    """A script-backed app in the default agent_invokes mode whose trigger
    names a known protocol via `invokes` (the atlas convention). This is the
    canonical migratable shape."""
    return {
        "id": "atlas-research",
        "name": "Atlas On-Demand Research",
        "invocation_mode": "agent_invokes",
        "blueprint": {
            "files": [
                {
                    "logical_name": "atlas_research",
                    "expected_location": "scripts/atlas_research.py",
                }
            ]
        },
        "event_triggers": [
            {
                "id": "at_mention",
                "source": "telegram",
                "audience": "members",
                "invokes": "atlas_research",
                "match": {
                    "channel": "telegram_group",
                    "pattern": r"@[A-Za-z][A-Za-z0-9_]{2,}\b",
                    "exclude_pattern": None,
                },
            }
        ],
    }


# ── Happy path ───────────────────────────────────────────────────────────────


def test_migrates_known_protocol_trigger():
    m = _agent_invokes_manifest()
    res = migrate_to_plugin_intercept(m)

    assert res["ok"] is True
    assert res["changed"] is True
    assert res["migrated_triggers"] == ["at_mention"]
    assert res["blocked_triggers"] == []

    out = res["manifest"]
    assert out["invocation_mode"] == "plugin_intercept"
    inv = out["event_triggers"][0]["invocation"]
    assert inv["script"] == "scripts/atlas_research.py"
    assert inv["stdout_protocol"] == "atlas_research"
    assert inv["on_failure"] in ("post_fallback", "silent")
    if inv["on_failure"] == "post_fallback":
        assert inv.get("fallback_text")  # non-empty


def test_migrated_manifest_passes_the_install_gate():
    """The whole point: the synthesized manifest must clear the same gate the
    forge / install dispatch enforce."""
    res = migrate_to_plugin_intercept(_agent_invokes_manifest())
    assert res["ok"] is True
    verdict = validate_bot_guidance(res["manifest"])
    assert verdict["ok"] is True
    assert verdict["invocation_mode"] == "plugin_intercept"


def test_input_manifest_is_not_mutated():
    m = _agent_invokes_manifest()
    snapshot = copy.deepcopy(m)
    migrate_to_plugin_intercept(m)
    assert m == snapshot  # migrator works on copies only


def test_resolves_script_from_existing_invocation_when_present():
    m = _agent_invokes_manifest()
    # Trigger already carries a partial invocation pointing elsewhere.
    m["event_triggers"][0]["invocation"] = {
        "script": "scripts/custom_path.py",
        "stdout_protocol": "atlas_research",
        "on_failure": "silent",
    }
    # `invokes` still atlas_research, so basename consistency holds.
    m["event_triggers"][0]["invokes"] = "custom_path"
    res = migrate_to_plugin_intercept(m)
    assert res["ok"] is True
    assert res["manifest"]["event_triggers"][0]["invocation"]["script"] == "scripts/custom_path.py"


def test_falls_back_to_scripts_convention_path():
    m = _agent_invokes_manifest()
    del m["blueprint"]  # no blueprint → convention path scripts/<invokes>.py
    res = migrate_to_plugin_intercept(m)
    assert res["ok"] is True
    assert res["manifest"]["event_triggers"][0]["invocation"]["script"] == "scripts/atlas_research.py"


# ── Idempotency ──────────────────────────────────────────────────────────────


def test_already_plugin_intercept_is_noop():
    m = _agent_invokes_manifest()
    first = migrate_to_plugin_intercept(m)
    assert first["ok"] and first["changed"]

    # Feed the migrated manifest back in — should be a no-op.
    second = migrate_to_plugin_intercept(first["manifest"])
    assert second["ok"] is True
    assert second["changed"] is False
    assert "already" in second["reason"].lower()


def test_already_plugin_intercept_but_broken_is_refused():
    m = _agent_invokes_manifest()
    m["invocation_mode"] = "plugin_intercept"
    # No invocation on the trigger → broken plugin_intercept contract.
    res = migrate_to_plugin_intercept(m)
    assert res["ok"] is False
    assert res["changed"] is False
    assert res["manifest"] is None


# ── Refusal cases (can't be made reliable) ───────────────────────────────────


def test_unknown_protocol_trigger_is_refused():
    m = _agent_invokes_manifest()
    # `invokes` is not a registered protocol and no invocation declares one.
    m["event_triggers"][0]["invokes"] = "send_invoice"
    m["blueprint"]["files"][0]["logical_name"] = "send_invoice"
    m["blueprint"]["files"][0]["expected_location"] = "scripts/send_invoice.py"
    res = migrate_to_plugin_intercept(m)
    assert res["ok"] is False
    assert res["changed"] is False
    assert res["manifest"] is None
    assert res["blocked_triggers"]
    assert res["blocked_triggers"][0]["id"] == "at_mention"
    # No half-migration: nothing persisted.
    assert "send_invoice" not in str(res["manifest"])


def test_no_triggers_is_refused():
    m = _agent_invokes_manifest()
    m["event_triggers"] = []
    res = migrate_to_plugin_intercept(m)
    assert res["ok"] is False
    assert "no structured message triggers" in res["reason"].lower()


def test_v7_arc_instance_is_refused_with_spec_message():
    m = _agent_invokes_manifest()
    m["manifest_shape"] = "v7-arc"
    res = migrate_to_plugin_intercept(m)
    assert res["ok"] is False
    assert res["changed"] is False
    assert "spec" in res["reason"].lower()


def test_subagent_mode_is_refused():
    m = _agent_invokes_manifest()
    m["invocation_mode"] = "subagent"
    res = migrate_to_plugin_intercept(m)
    assert res["ok"] is False
    assert res["manifest"] is None


def test_partial_migration_blocks_whole_manifest():
    """One migratable + one un-migratable trigger ⇒ whole migration refused
    (a plugin_intercept manifest with one un-contracted trigger is a
    build-blocker)."""
    m = _agent_invokes_manifest()
    m["event_triggers"].append({
        "id": "invoice_cmd",
        "source": "telegram",
        "audience": "members",
        "invokes": "send_invoice",  # unknown protocol
        "match": {"channel": "telegram_dm", "pattern": r"/invoice\b"},
    })
    res = migrate_to_plugin_intercept(m)
    assert res["ok"] is False
    assert res["manifest"] is None
    blocked_ids = [b["id"] for b in res["blocked_triggers"]]
    assert "invoice_cmd" in blocked_ids


def test_non_dict_input_is_refused():
    res = migrate_to_plugin_intercept(["not", "a", "manifest"])  # type: ignore[arg-type]
    assert res["ok"] is False
    assert res["manifest"] is None
