"""Tests for bot_guidance_freelance_validator.

Phase 2.1 of the agent-freelance-bypass spec
(internal/spec-agent-freelance-bypass-phase2-2026-06-06.md). The validator
catches three classes of manifest issue at install time:

1. invocation_mode value invalid (build_blocker)
2. invocation_mode == "plugin_intercept" but invocation contract broken
   (build_blocker)
3. At-risk-shaped manifest with agent_invokes mode and no event_triggers
   (warning — install is NOT blocked, but the Apps dashboard surfaces a
   "migrate to plugin_intercept" nudge)
"""

from __future__ import annotations

import pytest

from evolve_admin.applications.bot_guidance_freelance_validator import (
    validate_bot_guidance,
)


# ── Fixtures: canonical manifest shapes ──────────────────────────────────────


def _atlas_research_bot_guidance() -> list:
    """A bot_guidance block that trips the at-risk-shaped heuristic.

    Mirrors the actual atlas-on-demand-research manifest content (trimmed)
    so the test exercises the real-world marker set.
    """
    return [
        {
            "section": "Atlas — On-Demand Research",
            "content": (
                "When triggered, run exactly: python3 scripts/atlas_research.py "
                "/tmp/atlas-research-<msg.id>.json. The script's reply is the "
                "response. Do NOT freelance with your general tools if the "
                "script invocation fails."
            ),
        }
    ]


def _well_formed_trigger() -> dict:
    return {
        "id": "at_mention",
        "source": "telegram",
        "audience": "members",
        "invokes": "atlas_research",
        "match": {
            "channel": "telegram_group",
            "pattern": r"(?<![A-Za-z0-9_])@[A-Za-z][A-Za-z0-9_]{2,}\b",
            "exclude_pattern": None,
        },
        "invocation": {
            "script": "scripts/atlas_research.py",
            "request_file_template": "/tmp/atlas-research-{message_id}.json",
            "request_payload": {"mode": "ask", "query": "{message_text}"},
            "stdout_protocol": "atlas_research",
            "on_failure": "post_fallback",
            "fallback_text": "I couldn't research that — try again in a few minutes.",
        },
    }


# ── invocation_mode value gate ───────────────────────────────────────────────


def test_invocation_mode_invalid_value_blocks():
    res = validate_bot_guidance({
        "bot_guidance": [],
        "event_triggers": [],
        "invocation_mode": "garbage",
    })
    assert res["ok"] is False
    assert res["severity"] == "build_blocker"
    assert "garbage" in res["message"]


def test_invocation_mode_subagent_is_reserved():
    res = validate_bot_guidance({
        "bot_guidance": _atlas_research_bot_guidance(),
        "event_triggers": [_well_formed_trigger()],
        "invocation_mode": "subagent",
    })
    assert res["ok"] is False
    assert res["severity"] == "build_blocker"
    assert "deferred" in res["message"].lower()


def test_invocation_mode_default_is_agent_invokes():
    res = validate_bot_guidance({"bot_guidance": []})
    assert res["invocation_mode"] == "agent_invokes"


# ── agent_invokes (default): at-risk-shaped surfaces as a warning ───────────


def test_at_risk_shaped_with_no_triggers_returns_warning():
    """The current atlas posture pre-Phase-2.4 — at-risk-shaped, agent_invokes,
    no triggers. The validator must NOT block install (``ok`` stays True) but
    escalates to ``warning`` so the Apps dashboard nudges the safer mode."""
    res = validate_bot_guidance({
        "bot_guidance": _atlas_research_bot_guidance(),
        "event_triggers": [],
        "invocation_mode": "agent_invokes",
    })
    assert res["ok"] is True
    assert res["severity"] == "warning"
    # The escalation must stop at warning — escalating to build_blocker would
    # refuse re-validation of the existing agent_invokes apps.
    assert res["severity"] != "build_blocker"
    assert res["is_at_risk"] is True
    assert len(res["markers"]) >= 1
    assert "plugin_intercept" in res["message"]


def test_existing_agent_invokes_apps_are_never_blocked():
    """Regression guard for the escalation: the at-risk-shaped apps that ship
    in agent_invokes (atlas-on-demand-research, atlas-article-capture, and
    other on-demand task apps) must keep installing / re-validating. A warning
    is fine; a build_blocker is not — it would wedge their re-validation."""
    res = validate_bot_guidance({
        "bot_guidance": _atlas_research_bot_guidance(),
        "event_triggers": [],
        "invocation_mode": "agent_invokes",
    })
    assert res["ok"] is True
    assert res["severity"] != "build_blocker"


def test_clean_manifest_with_no_at_risk_markers_returns_clean():
    """A docs-only or cron-only manifest doesn't trip the heuristic."""
    res = validate_bot_guidance({
        "bot_guidance": [
            {"section": "Setup", "content": "Run the cron job at 9am daily."}
        ],
        "event_triggers": [],
    })
    assert res["ok"] is True
    assert res["severity"] == "info"
    assert res["is_at_risk"] is False
    assert res["markers"] == []


def test_at_risk_markers_match_pr_2192_canonical_phrasing():
    """The validator must recognize the language PR #2192 used so the
    fallback / detection chain works with the real atlas manifests."""
    res = validate_bot_guidance({
        "bot_guidance": [
            {
                "section": "X",
                "content": "Do NOT freelance with your general tools if the script invocation fails.",
            }
        ],
        "event_triggers": [],
    })
    assert res["is_at_risk"] is True


# ── file-reference at-risk detection (sf-b4) ─────────────────────────────────
#
# The prose markers above are narrow (imperative hard-stop phrasing only), and
# on v7-arc instances bot_guidance is not overlaid during hydration — so the
# prose path alone never warned team-bot-c's Task Management, whose scripts/tasks.py
# failed live, leaked a raw "(agent) failed", and confabulated success. The
# fix also treats a registered bot-local script reference in the manifest's
# file fields (evidence_files / files / realized_files) as at-risk-shaped.


def _v7arc_instance_shape(**overrides) -> dict:
    """A v7-arc instance shape: no bot_guidance / invocation_mode (those live on
    the Spec and are NOT overlaid during hydration), no event_triggers; the
    script reference rides on evidence_files / realized_files."""
    base = {
        "bot_guidance": None,
        "invocation_mode": None,
        "event_triggers": None,
        "evidence_files": [],
        "realized_files": [],
    }
    base.update(overrides)
    return base


def test_team_bot_c_task_management_shape_warns():
    """Regression for the live incident: team-bot-c's Task Management is a v7-arc
    instance with no bot_guidance prose, no event_triggers, and scripts/tasks.py
    on its evidence/realized files. The prose path is blind to it; the
    file-reference path must surface the warning + migrate nudge."""
    res = validate_bot_guidance(_v7arc_instance_shape(
        evidence_files=[
            "AGENTS.md (Self-Tasking section)",
            "AGENTS.md (Task Management section)",
            "scripts/tasks.py",
        ],
        realized_files=[{"path": "scripts/tasks.py"}],
        files=[{"path": "scripts/tasks.py"}],
    ))
    assert res["ok"] is True
    assert res["severity"] == "warning"
    assert res["severity"] != "build_blocker"   # never blocks an existing install
    assert res["is_at_risk"] is True
    assert "scripts/tasks.py" in res["message"]
    assert "plugin_intercept" in res["message"]
    assert "script-file:scripts/tasks.py" in res["markers"]


def test_script_reference_only_in_evidence_files_warns():
    res = validate_bot_guidance(_v7arc_instance_shape(
        evidence_files=["scripts/run_report.py"],
    ))
    assert res["severity"] == "warning"
    assert res["is_at_risk"] is True


def test_script_reference_only_in_realized_files_warns():
    res = validate_bot_guidance(_v7arc_instance_shape(
        realized_files=[{"path": "scripts/run_report.py"}],
    ))
    assert res["severity"] == "warning"
    assert res["is_at_risk"] is True


def test_legacy_scripts_shell_reference_warns():
    """legacy-scripts/*.sh counts — a failed shell helper leaks the same way."""
    res = validate_bot_guidance(_v7arc_instance_shape(
        evidence_files=["legacy-scripts/setup_dropbox_selective_sync.sh"],
    ))
    assert res["severity"] == "warning"
    assert "legacy-scripts/setup_dropbox_selective_sync.sh" in res["message"]


def test_script_reference_in_bot_guidance_prose_warns():
    """A legacy (non-v7) manifest whose script reference appears only in
    documentation-style bot_guidance prose — no imperative marker — is still
    caught by the file-reference scan over the prose content."""
    res = validate_bot_guidance({
        "bot_guidance": [
            {"section": "Files", "content":
                "- `scripts/tasks.py` — Primary entry point; CLI tool with argparse subcommands."}
        ],
        "event_triggers": [],
        "invocation_mode": "agent_invokes",
    })
    assert res["severity"] == "warning"
    assert res["is_at_risk"] is True
    assert "scripts/tasks.py" in res["message"]


def test_data_files_only_do_not_warn():
    """Memory System / Daily Briefing shape: evidence_files are docs/data, not a
    bot-local script. Must NOT warn — their '⚠ 1 warning' badge is an unrelated
    coherence finding, and the freelance validator must stay quiet."""
    res = validate_bot_guidance(_v7arc_instance_shape(
        evidence_files=[
            "AGENTS.md (Memory section)",
            "operations/status-reports/2026-03-15-daily-status.md",
            "property/.dropbox_snapshot.txt",
            "manifests/app-memory-system.json",
        ],
        realized_files=[{"path": "operations/status-reports/x.md"}],
    ))
    assert res["severity"] == "info"
    assert res["is_at_risk"] is False
    assert res["markers"] == []


def test_py_file_outside_scripts_dir_does_not_warn():
    """Directory-anchored: a .py path NOT under scripts/ or legacy-scripts/ is
    not a bot-local invoked script (false-positive guard)."""
    res = validate_bot_guidance(_v7arc_instance_shape(
        evidence_files=["property/helper.py", "src/lib/util.py"],
    ))
    assert res["severity"] == "info"
    assert res["is_at_risk"] is False


def test_word_prefixed_scripts_dir_does_not_warn():
    """The component anchor rejects ``myscripts/`` / ``subscripts/`` — only a
    real ``scripts/`` or ``legacy-scripts/`` component matches."""
    res = validate_bot_guidance(_v7arc_instance_shape(
        evidence_files=["myscripts/foo.py", "subscripts/bar.sh"],
    ))
    assert res["severity"] == "info"
    assert res["is_at_risk"] is False


def test_script_backed_app_migrated_to_plugin_intercept_stops_nagging():
    """Once the operator migrates the same script-backed app to plugin_intercept
    with a valid trigger, the warning resolves to info — the nudge goes away."""
    res = validate_bot_guidance({
        "invocation_mode": "plugin_intercept",
        "evidence_files": ["scripts/tasks.py"],
        "realized_files": [{"path": "scripts/tasks.py"}],
        "event_triggers": [_well_formed_trigger()],
    })
    assert res["ok"] is True
    assert res["severity"] == "info"
    assert "Layer C enforcement" in res["message"]


def test_script_backed_app_with_declared_triggers_not_warned():
    """The trigger_count == 0 guard is preserved: an agent_invokes app that has
    declared structured triggers is partially structured (audit can detect its
    bypasses) and is not nudged by this path."""
    res = validate_bot_guidance({
        "invocation_mode": "agent_invokes",
        "evidence_files": ["scripts/tasks.py"],
        "event_triggers": [_well_formed_trigger()],
    })
    assert res["severity"] != "warning"


def test_manifest_object_script_reference_warns():
    """Attribute-access manifests (ApplicationManifest-like) hit the file path
    scan too."""

    class FakeManifest:
        bot_guidance = None
        invocation_mode = "agent_invokes"
        event_triggers = []
        evidence_files = ["scripts/tasks.py"]
        realized_files = [{"path": "scripts/tasks.py"}]

    res = validate_bot_guidance(FakeManifest())
    assert res["severity"] == "warning"
    assert res["is_at_risk"] is True


def test_legacy_evidence_dict_files_shape_warns():
    """The legacy ``evidence={"files": [...]}`` shape is scanned too."""
    res = validate_bot_guidance({
        "bot_guidance": None,
        "invocation_mode": "agent_invokes",
        "event_triggers": [],
        "evidence": {"files": ["scripts/tasks.py"]},
    })
    assert res["severity"] == "warning"
    assert res["is_at_risk"] is True


# ── plugin_intercept opt-in gate ────────────────────────────────────────────


def test_plugin_intercept_with_well_formed_trigger_passes():
    res = validate_bot_guidance({
        "bot_guidance": _atlas_research_bot_guidance(),
        "event_triggers": [_well_formed_trigger()],
        "invocation_mode": "plugin_intercept",
    })
    assert res["ok"] is True
    assert res["severity"] == "info"
    assert res["trigger_count"] == 1
    assert "Layer C enforcement" in res["message"]


def test_plugin_intercept_with_empty_triggers_blocks():
    res = validate_bot_guidance({
        "bot_guidance": _atlas_research_bot_guidance(),
        "event_triggers": [],
        "invocation_mode": "plugin_intercept",
    })
    assert res["ok"] is False
    assert res["severity"] == "build_blocker"
    assert "empty or absent" in res["errors"][0]


def test_plugin_intercept_with_missing_invocation_blocks():
    trigger = _well_formed_trigger()
    del trigger["invocation"]
    res = validate_bot_guidance({
        "bot_guidance": _atlas_research_bot_guidance(),
        "event_triggers": [trigger],
        "invocation_mode": "plugin_intercept",
    })
    assert res["ok"] is False
    assert res["severity"] == "build_blocker"
    assert any("requires every" in e for e in res["errors"])


def test_plugin_intercept_with_broken_pattern_regex_blocks():
    trigger = _well_formed_trigger()
    trigger["match"]["pattern"] = "("  # unbalanced — re.compile raises
    res = validate_bot_guidance({
        "bot_guidance": _atlas_research_bot_guidance(),
        "event_triggers": [trigger],
        "invocation_mode": "plugin_intercept",
    })
    assert res["ok"] is False
    assert any("not a valid Python regex" in e for e in res["errors"])


def test_plugin_intercept_with_broken_exclude_pattern_blocks():
    trigger = _well_formed_trigger()
    trigger["match"]["exclude_pattern"] = "[unclosed"
    res = validate_bot_guidance({
        "bot_guidance": _atlas_research_bot_guidance(),
        "event_triggers": [trigger],
        "invocation_mode": "plugin_intercept",
    })
    assert res["ok"] is False
    assert any("exclude_pattern" in e for e in res["errors"])


def test_plugin_intercept_with_unknown_stdout_protocol_blocks():
    trigger = _well_formed_trigger()
    trigger["invocation"]["stdout_protocol"] = "myapp_custom"
    res = validate_bot_guidance({
        "bot_guidance": _atlas_research_bot_guidance(),
        "event_triggers": [trigger],
        "invocation_mode": "plugin_intercept",
    })
    assert res["ok"] is False
    assert any("not registered in plugin code" in e for e in res["errors"])
    # Error must mention the known protocols so the operator can pick.
    assert any("atlas_research" in e for e in res["errors"])


def test_plugin_intercept_post_fallback_without_fallback_text_blocks():
    trigger = _well_formed_trigger()
    trigger["invocation"]["fallback_text"] = ""
    res = validate_bot_guidance({
        "bot_guidance": _atlas_research_bot_guidance(),
        "event_triggers": [trigger],
        "invocation_mode": "plugin_intercept",
    })
    assert res["ok"] is False
    assert any("post_fallback" in e and "fallback_text" in e for e in res["errors"])


def test_plugin_intercept_on_failure_silent_does_not_require_fallback_text():
    trigger = _well_formed_trigger()
    trigger["invocation"]["on_failure"] = "silent"
    del trigger["invocation"]["fallback_text"]
    res = validate_bot_guidance({
        "bot_guidance": _atlas_research_bot_guidance(),
        "event_triggers": [trigger],
        "invocation_mode": "plugin_intercept",
    })
    assert res["ok"] is True


def test_plugin_intercept_invokes_script_basename_mismatch_blocks():
    trigger = _well_formed_trigger()
    trigger["invokes"] = "atlas_capture"          # claims one script
    trigger["invocation"]["script"] = "scripts/atlas_research.py"  # but points at another
    res = validate_bot_guidance({
        "bot_guidance": _atlas_research_bot_guidance(),
        "event_triggers": [trigger],
        "invocation_mode": "plugin_intercept",
    })
    assert res["ok"] is False
    assert any("don't appear related" in e for e in res["errors"])


def test_plugin_intercept_collects_multiple_errors():
    """Errors should accumulate across triggers, not short-circuit on first."""
    trig_a = _well_formed_trigger()
    trig_a["id"] = "trig_a"
    trig_a["match"]["pattern"] = "("
    trig_b = _well_formed_trigger()
    trig_b["id"] = "trig_b"
    trig_b["invocation"]["stdout_protocol"] = "myapp_custom"
    res = validate_bot_guidance({
        "bot_guidance": _atlas_research_bot_guidance(),
        "event_triggers": [trig_a, trig_b],
        "invocation_mode": "plugin_intercept",
    })
    assert res["ok"] is False
    # Both triggers should surface their own error.
    assert any("trig_a" in e for e in res["errors"])
    assert any("trig_b" in e for e in res["errors"])


# ── Tolerance: non-dict triggers, missing fields, attribute-access manifest ─


def test_non_dict_trigger_does_not_crash():
    res = validate_bot_guidance({
        "bot_guidance": _atlas_research_bot_guidance(),
        "event_triggers": ["not-a-dict"],
        "invocation_mode": "plugin_intercept",
    })
    assert res["ok"] is False
    assert any("not an object" in e for e in res["errors"])


def test_missing_event_triggers_treated_as_empty():
    res = validate_bot_guidance({
        "bot_guidance": _atlas_research_bot_guidance(),
        # no event_triggers key
        "invocation_mode": "agent_invokes",
    })
    assert res["ok"] is True
    assert res["trigger_count"] == 0


def test_manifest_like_object_attribute_access():
    """The validator accepts ApplicationManifest-like objects (attribute
    access) so caller doesn't have to convert to dict first."""

    class FakeManifest:
        bot_guidance = []
        event_triggers = [_well_formed_trigger()]
        invocation_mode = "plugin_intercept"

    res = validate_bot_guidance(FakeManifest())
    assert res["ok"] is True
    assert res["trigger_count"] == 1


def test_application_manifest_dataclass_round_trips_phase2_fields():
    """Regression test for the gap caught during Phase 2.2 design.

    ApplicationManifest.from_dict() filters keys to the dataclass field
    set. If bot_guidance / event_triggers / invocation_mode are missing
    from the dataclass, they're silently dropped on every manifest load —
    and the forge_engine validator (which is called with the dataclass
    instance) gets None / empty defaults back and silently passes every
    manifest. This test pins those three fields to the dataclass so a
    future field rename or removal trips a clear CI failure.
    """
    from evolve_admin.applications.manifest import ApplicationManifest

    fields = ApplicationManifest.__dataclass_fields__
    assert "bot_guidance" in fields, (
        "ApplicationManifest must declare bot_guidance — without it, "
        "from_dict drops the field and the Phase 2 validator misfires."
    )
    assert "event_triggers" in fields, (
        "ApplicationManifest must declare event_triggers — without it, "
        "from_dict drops the field and the Phase 2 validator misfires."
    )
    assert "invocation_mode" in fields, (
        "ApplicationManifest must declare invocation_mode — without it, "
        "from_dict drops the field and the Phase 2 validator misfires."
    )

    # End-to-end round-trip: dict → manifest → dict preserves the values.
    payload = {
        "id": "test-app",
        "name": "Test App",
        "bot_id": "team_bot_a",
        "bot_guidance": _atlas_research_bot_guidance(),
        "event_triggers": [_well_formed_trigger()],
        "invocation_mode": "plugin_intercept",
    }
    m = ApplicationManifest.from_dict(payload)
    assert m.bot_guidance == payload["bot_guidance"]
    assert m.event_triggers == payload["event_triggers"]
    assert m.invocation_mode == "plugin_intercept"

    # Validator pulled directly off the dataclass should see the contract.
    res = validate_bot_guidance(m)
    assert res["ok"] is True
    assert res["invocation_mode"] == "plugin_intercept"
    assert res["trigger_count"] == 1


def test_completely_empty_pkg_does_not_crash():
    res = validate_bot_guidance({})
    assert res["ok"] is True  # nothing to block; clean by default
    assert res["invocation_mode"] == "agent_invokes"
    assert res["trigger_count"] == 0


# ── Schema enforces what the validator surfaces ─────────────────────────────


def test_schema_declares_invocation_mode_enum():
    """The top-level invocation_mode enum on the v7 spec schema must match
    the validator's accepted set so a JSON-schema pre-check and the
    validator never disagree.
    """
    import json
    from pathlib import Path

    schema_path = Path(__file__).resolve().parents[3] / "docs" / "schemas" / "manifest-v7-spec.schema.json"
    schema = json.loads(schema_path.read_text())
    enum = schema["properties"]["invocation_mode"]["enum"]
    assert set(enum) == {"agent_invokes", "plugin_intercept", "subagent"}


def test_schema_event_triggers_invocation_stdout_protocol_enum_matches_validator():
    import json
    from pathlib import Path
    from evolve_admin.applications.bot_guidance_freelance_validator import (
        _KNOWN_STDOUT_PROTOCOLS,
    )

    schema_path = Path(__file__).resolve().parents[3] / "docs" / "schemas" / "manifest-v7-spec.schema.json"
    schema = json.loads(schema_path.read_text())
    inv = schema["properties"]["event_triggers"]["items"]["properties"]["invocation"]
    enum = inv["properties"]["stdout_protocol"]["enum"]
    assert set(enum) == set(_KNOWN_STDOUT_PROTOCOLS)
