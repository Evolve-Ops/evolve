"""Unit tests for the v23 producer-surface assertion.

Companion to PR #2476's platform-files defense. That PR archives the
specific Session-Turn-Logs shape on the next scan; this audit check
catches the general failure mode (manifest with no machinery to invoke
it) wherever it appears, regardless of whether the file footprint is
platform-written.

Pure function over (manifest, ctx). No filesystem, no Signal store.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))

from app_audit_structural import (  # noqa: E402
    SEVERITY_INFO,
    SEVERITY_MAJOR,
    _declares_delivery_intent,
    _has_data_realized_files,
    _has_script_realized_file,
    _is_observational_instance,
    check_app_has_producer_surface,
    producer_surface_kinds,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


def _empty_shell() -> dict:
    """The Session-Turn-Logs shape: name + hint_words, nothing invocable."""
    return {
        "id": "i-34cfcab1",
        "name": "Session Turn Logs",
        "description": "Session Turn Logs",
        "scheduled_actions": [],
        "heartbeat_evidence": {},
        "cron_evidence": {},
        "crons": [],
        "event_triggers": [],
        "interface_contract": {},
        "usage": {
            "model": "user-initiated",
            "trigger_recognition": {
                "hint_words": [
                    "Session Turn Logs", "session", "turn", "logs",
                ],
            },
        },
    }


# ── producer_surface_kinds ──────────────────────────────────────────────────


def test_surface_kinds_empty_for_shell() -> None:
    assert producer_surface_kinds(_empty_shell()) == []


def test_surface_kinds_detects_scheduled_actions_with_real_install() -> None:
    """v23.1: only counts when install.{file,plist_label,command} non-empty."""
    m = _empty_shell()
    m["scheduled_actions"] = [{
        "id": "log-daily",
        "trigger": {"kind": "heartbeat"},
        "install": {"file": "HEARTBEAT.md", "section_anchor": "## Daily"},
    }]
    assert "scheduled_actions" in producer_surface_kinds(m)


def test_surface_kinds_ignores_scheduled_actions_with_vacuous_install() -> None:
    """v23.1: pre-scanner stub entries (mechanism=unknown, install all
    None) MUST NOT count. Pod survey 2026-06-09 found these on 29 of 37
    manifests; counting them caused systematic audit underfire."""
    m = _empty_shell()
    m["scheduled_actions"] = [
        {"id": "vacuous-1", "mechanism": "unknown", "trigger": {"kind": "heartbeat"},
         "install": {"file": None, "plist_label": None, "command": None}},
        {"id": "vacuous-2", "mechanism": "unknown", "trigger": {"kind": "heartbeat"},
         "install": {}},
    ]
    assert "scheduled_actions" not in producer_surface_kinds(m)


def test_surface_kinds_mixed_vacuous_and_real_counts_as_real() -> None:
    """A manifest with N vacuous and 1 real entry still has the surface."""
    m = _empty_shell()
    m["scheduled_actions"] = [
        {"id": "vacuous", "mechanism": "unknown", "install": {}},
        {"id": "real", "mechanism": "launchd",
         "install": {"plist_label": "com.bot.real.check"}},
    ]
    assert "scheduled_actions" in producer_surface_kinds(m)


def test_surface_kinds_counts_command_only_install() -> None:
    """install.command alone (no file, no label) is the last-resort
    fallback the scanner emits for hand-installed crons."""
    m = _empty_shell()
    m["scheduled_actions"] = [{
        "id": "command-only",
        "install": {"command": "python3 ops/foo.py"},
    }]
    assert "scheduled_actions" in producer_surface_kinds(m)


def test_surface_kinds_detects_heartbeat_evidence_only_when_anchors_present() -> None:
    m = _empty_shell()
    # Empty dict shouldn't count
    m["heartbeat_evidence"] = {}
    assert producer_surface_kinds(m) == []
    # Dict without section_anchors shouldn't count either
    m["heartbeat_evidence"] = {"file_path": "HEARTBEAT.md"}
    assert producer_surface_kinds(m) == []
    # With anchors it counts
    m["heartbeat_evidence"] = {
        "file_path": "HEARTBEAT.md",
        "section_anchors": ["Daily Logging"],
    }
    assert "heartbeat_evidence" in producer_surface_kinds(m)


def test_surface_kinds_detects_cron_evidence_only_when_labels_present() -> None:
    m = _empty_shell()
    m["cron_evidence"] = {}
    assert producer_surface_kinds(m) == []
    m["cron_evidence"] = {"labels": []}
    assert producer_surface_kinds(m) == []
    m["cron_evidence"] = {"labels": ["ai.openclaw.personal-bot.daily-logging"]}
    assert "cron_evidence" in producer_surface_kinds(m)


def test_surface_kinds_detects_crons() -> None:
    m = _empty_shell()
    m["crons"] = [{"schedule": "0 0 * * *", "script": "ops/daily.py"}]
    assert "crons" in producer_surface_kinds(m)


def test_surface_kinds_detects_event_triggers() -> None:
    m = _empty_shell()
    m["event_triggers"] = [{"event": "user_message", "match": "log workout"}]
    assert "event_triggers" in producer_surface_kinds(m)


def test_surface_kinds_detects_cli() -> None:
    m = _empty_shell()
    m["interface_contract"] = {"cli": [{"command": "personal-bot-app run"}]}
    assert "interface_contract.cli" in producer_surface_kinds(m)


def test_surface_kinds_returns_all_present_kinds() -> None:
    """Multiple surfaces are all reported."""
    m = _empty_shell()
    m["scheduled_actions"] = [{
        "id": "x", "install": {"file": "AGENTS.md", "section_anchor": "## X"},
    }]
    m["heartbeat_evidence"] = {"file_path": "HEARTBEAT.md", "section_anchors": ["A"]}
    m["interface_contract"] = {"cli": [{"command": "x"}]}
    kinds = producer_surface_kinds(m)
    assert "scheduled_actions" in kinds
    assert "heartbeat_evidence" in kinds
    assert "interface_contract.cli" in kinds


# ── passive-app calibration helpers ─────────────────────────────────────────


def test_is_observational_instance_true_for_observational_origin() -> None:
    m = {"manifest_shape": "v7-arc",
         "provenance": {"manifest_origin": "observational"}}
    assert _is_observational_instance(m) is True


def test_is_observational_instance_true_for_scanner_created_by() -> None:
    m = {"manifest_shape": "v7-arc", "provenance": {"created_by": "scanner"}}
    assert _is_observational_instance(m) is True


def test_is_observational_instance_false_for_forge_origin() -> None:
    m = {"manifest_shape": "v7-arc",
         "provenance": {"manifest_origin": "forge", "installed_by": "forge"}}
    assert _is_observational_instance(m) is False


def test_is_observational_instance_false_for_migration() -> None:
    m = {"manifest_shape": "v7-arc",
         "provenance": {"installed_by": "migration", "spec_id": "p-abc"}}
    assert _is_observational_instance(m) is False


def test_is_observational_instance_false_for_non_v7_arc() -> None:
    m = {"manifest_shape": "v6", "provenance": {"created_by": "scanner"}}
    assert _is_observational_instance(m) is False


def test_has_data_realized_files_true_for_data_file() -> None:
    assert _has_data_realized_files({"realized_files": [{"path": "memory/x.md"}]}) is True


def test_has_data_realized_files_false_for_empty() -> None:
    assert _has_data_realized_files({"realized_files": []}) is False
    assert _has_data_realized_files({}) is False


def test_has_data_realized_files_false_for_own_manifest_only() -> None:
    """A manifest whose only realized file is a manifest json is bookkeeping,
    not attributed user data."""
    m = {"realized_files": [{"path": "manifests/app-daily-logging.json"}]}
    assert _has_data_realized_files(m) is False


def test_has_data_realized_files_false_for_registry_of_manifests() -> None:
    m = {"realized_files": [
        {"path": "manifests/app-a.json"}, {"path": "manifests/app-b.json"}]}
    assert _has_data_realized_files(m) is False


def test_has_data_realized_files_tolerates_string_and_none_entries() -> None:
    m = {"realized_files": ["memory/notes.md", None, {"path": "x.md"}]}
    assert _has_data_realized_files(m) is True


def test_declares_delivery_intent_true_for_delivery_contract() -> None:
    m = {"scheduled_actions": [{"id": "x", "delivery_contract": {"mechanism": "m"}}]}
    assert _declares_delivery_intent(m) is True


def test_declares_delivery_intent_true_for_outputs() -> None:
    m = {"outputs": [{"kind": "summary", "channel": "telegram"}]}
    assert _declares_delivery_intent(m) is True


def test_declares_delivery_intent_false_for_empty_outputs_and_no_contract() -> None:
    m = {"outputs": [], "scheduled_actions": [{"id": "x", "install": {}}]}
    assert _declares_delivery_intent(m) is False


# ── check_app_has_producer_surface ───────────────────────────────────────────


def test_empty_shell_fires_app_no_producer_surface() -> None:
    """The Session-Turn-Logs shape: hint_words alone, nothing invocable.

    The L3 platform-files sweep also covers this specific manifest, but
    the audit must fire INDEPENDENTLY so new variants (non-platform
    file footprints) are caught too.
    """
    findings = check_app_has_producer_surface(_empty_shell(), {})
    assert len(findings) == 1
    assert findings[0].assertion_id == "app_no_producer_surface"
    assert findings[0].severity == SEVERITY_MAJOR
    assert "scheduled_actions" in findings[0].evidence["fields_checked"]


def test_hint_words_alone_do_not_count_as_producer_surface() -> None:
    """Hint words tell the renderer 'show this app'; they don't make
    the app invocable. Pin this so a future shortcut that aliases
    hint_words → 'app has surface' fails this test."""
    m = _empty_shell()
    m["usage"]["trigger_recognition"]["hint_words"] = ["foo"] * 20
    findings = check_app_has_producer_surface(m, {})
    assert len(findings) == 1


def test_user_routed_model_alone_does_not_count_as_producer_surface() -> None:
    """usage.model = 'user-initiated' without backing machinery (CLI,
    event trigger, etc.) is not a producer surface — the migration
    used to set this field blanket-defaulted on every legacy manifest."""
    m = _empty_shell()
    m["usage"] = {"model": "user-initiated"}
    assert check_app_has_producer_surface(m, {}) != []


def test_scheduled_actions_satisfies_check() -> None:
    """Daily-Logging-shape: heartbeat-anchored standing instruction
    drives the app — that's a real producer surface."""
    m = _empty_shell()
    m["scheduled_actions"] = [{
        "id": "daily-log-init",
        "trigger": {"kind": "heartbeat", "evidence_locator": "Daily Logging"},
        "install": {"file": "HEARTBEAT.md", "section_anchor": "## Daily Logging"},
    }]
    m["heartbeat_evidence"] = {
        "file_path": "HEARTBEAT.md",
        "section_anchors": ["Daily Logging"],
    }
    assert check_app_has_producer_surface(m, {}) == []


def test_audit_logging_shape_fires_v23_check() -> None:
    """security-bot/audit-logging: 6 scheduled_actions[] all with empty
    install blocks. The Phase 1 tighten reclassifies these as
    no-surface, so app_no_producer_surface fires here even though
    scheduled_actions[] is non-empty. Counterpart to L3 archival in
    scanner.py."""
    m = _empty_shell()
    m["scheduled_actions"] = [
        {"id": f"audit-logging-action-{i}", "mechanism": "unknown",
         "install": {"file": None, "plist_label": None, "command": None}}
        for i in range(6)
    ]
    # And the manifest carries plausible-sounding identity prose, matching
    # what the survey observed. The audit MUST still fire — identity isn't
    # a producer surface.
    m["identity"] = {
        "purpose": "Maintain a tamper-evident, compliance-ready audit trail.",
        "scope_includes": ["Capture audit records"],
    }
    findings = check_app_has_producer_surface(m, {})
    assert len(findings) == 1
    assert findings[0].assertion_id == "app_no_producer_surface"


def test_cli_alone_satisfies_check() -> None:
    """An app invoked solely by CLI command is fine — the CLI is the
    surface even with no other triggers."""
    m = _empty_shell()
    m["interface_contract"] = {"cli": [{"command": "my-tool run"}]}
    assert check_app_has_producer_surface(m, {}) == []


def test_cron_alone_satisfies_check() -> None:
    m = _empty_shell()
    m["crons"] = [{"schedule": "0 6 * * *", "script": "ops/morning.py"}]
    assert check_app_has_producer_surface(m, {}) == []


def test_event_triggers_alone_satisfies_check() -> None:
    m = _empty_shell()
    m["event_triggers"] = [{"event": "calendar_update"}]
    assert check_app_has_producer_surface(m, {}) == []


def test_cron_evidence_label_satisfies_check() -> None:
    """Layer B populates cron_evidence with launchd labels — this test
    ensures Layer A trusts that as a producer surface."""
    m = _empty_shell()
    m["cron_evidence"] = {"labels": ["ai.openclaw.personal-bot.heartbeat"]}
    assert check_app_has_producer_surface(m, {}) == []


def test_retired_manifest_does_not_fire() -> None:
    """A retired manifest's machinery is expected to be gone."""
    m = _empty_shell()
    m["status"] = "retired"
    assert check_app_has_producer_surface(m, {}) == []


def test_dismissed_manifest_does_not_fire() -> None:
    m = _empty_shell()
    m["status"] = "dismissed"
    assert check_app_has_producer_surface(m, {}) == []


def test_archived_manifest_does_not_fire() -> None:
    m = _empty_shell()
    m["status"] = "archived"
    assert check_app_has_producer_surface(m, {}) == []


def test_active_manifest_with_no_surface_fires() -> None:
    m = _empty_shell()
    m["status"] = "active"
    assert check_app_has_producer_surface(m, {}) != []


def test_finding_summary_lists_the_checked_fields() -> None:
    """Operator-facing finding text should name the fields we looked at
    so the fix is obvious."""
    findings = check_app_has_producer_surface(_empty_shell(), {})
    summary = findings[0].summary
    assert "scheduled_actions" in summary
    assert "heartbeat_evidence" in summary
    assert "cron_evidence" in summary


def test_check_listed_in_default_assertions() -> None:
    """Regression guard: forgetting to register the check in
    DEFAULT_ASSERTIONS means the audit runner never invokes it."""
    from app_audit_structural import DEFAULT_ASSERTIONS
    assert check_app_has_producer_surface in DEFAULT_ASSERTIONS


def test_assertion_id_listed_in_assertion_ids() -> None:
    from app_audit_structural import ASSERTION_IDS
    assert "app_no_producer_surface" in ASSERTION_IDS


# ── Scanner-shell downgrade ─────────────────────────────────────────────────
#
# A manifest the scanner just minted (``source_detail`` starts with
# ``scan:``) with no realized files yet is a scanner-attribution failure,
# not an operator-actionable app bug. The check stays firing so the
# trail records it, but at SEVERITY_INFO so it doesn't fan out into
# the operator's alert lane.


def test_scanner_shell_downgrades_to_info() -> None:
    m = _empty_shell()
    m["source_detail"] = "scan:2026-06-09T00:00:00Z"
    m["instance"] = {"realized_files": []}
    findings = check_app_has_producer_surface(m, {})
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_INFO
    assert findings[0].evidence.get("scanner_shell") is True


def test_scanner_shell_with_realized_files_stays_major() -> None:
    """Scanner-minted but with actual files attributed — the no-surface
    finding is still real (manifest names something but doesn't say
    how to drive it). Severity stays major."""
    m = _empty_shell()
    m["source_detail"] = "scan:2026-06-09T00:00:00Z"
    m["instance"] = {"realized_files": [{"path": "scripts/foo.py"}]}
    findings = check_app_has_producer_surface(m, {})
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_MAJOR
    assert findings[0].evidence.get("scanner_shell") is False


def test_non_scanner_source_with_empty_realized_files_stays_major() -> None:
    """User-created / gallery-installed manifests aren't scanner shells
    even when realized_files is empty; the operator authored those and
    can fix them."""
    m = _empty_shell()
    m["source_detail"] = "forge:abc123"
    m["instance"] = {"realized_files": []}
    findings = check_app_has_producer_surface(m, {})
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_MAJOR


def test_pre_v7_manifest_without_instance_stays_major() -> None:
    """Pre-v7 manifests have no ``instance`` block — no realized_files
    signal available, so they don't qualify for the scanner-shell
    downgrade even when source_detail starts with scan."""
    m = _empty_shell()
    m["source_detail"] = "scan:2026-06-09T00:00:00Z"
    # No instance key
    findings = check_app_has_producer_surface(m, {})
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_MAJOR


def test_v7_arc_observational_instance_downgrades_to_info() -> None:
    """Team-bot-a-style observational v7-arc Instance: scanner-minted
    shell with top-level manifest_shape=v7-arc + observational provenance
    + empty realized_files. Pod survey 2026-06-09 found 5 of these on
    team-bot-a, all with spec_id: None. They should downgrade to info so
    the operator's alert lane isn't flooded by the scanner's attribution
    failure."""
    m = _empty_shell()
    m["manifest_shape"] = "v7-arc"
    m["provenance"] = {
        "manifest_origin": "observational",
        "created_by": "scanner",
        "spec_id": None,
    }
    m["realized_files"] = []
    findings = check_app_has_producer_surface(m, {})
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_INFO
    assert findings[0].evidence.get("scanner_shell") is True


def test_v7_arc_observational_with_data_files_is_passive_app_no_fire() -> None:
    """Passive-app calibration (2026-06-13, companion to R1 verifier-noise
    PR #2828). RECALIBRATED — this case previously fired major.

    An observational scanner-minted Instance with attributed (non-script)
    data files is a working passive data store — a logger / tracker /
    workspace the bot maintains conversationally. It was discovered by
    observation, never designed with producer machinery, and declares no
    delivery obligation. "No producer surface" is inherent to it, not a
    defect. Pod survey 2026-06-13: 13 of 29 firing app_no_producer_surface
    signals were this exact shape (a Home Repairs Log, a Job Search Tracker,
    a Work Order Tracker, a Heartbeat Monitoring app, …). The check must NOT
    fire so those Signals sweep-resolve."""
    m = _empty_shell()
    m["manifest_shape"] = "v7-arc"
    m["provenance"] = {"manifest_origin": "observational"}
    m["realized_files"] = [{"path": "data/vineyard.db", "file_id": "f-abc"}]
    assert check_app_has_producer_surface(m, {}) == []


def test_v7_arc_observational_declaring_delivery_contract_stays_major() -> None:
    """The "supposed to deliver but produces nothing" case is NOT
    suppressed. An observational Instance with data files but a declared
    delivery_contract on a (vacuous-install) scheduled_action has a real,
    operator-actionable gap: it promises delivery and has no surface to
    fulfill it. Severity stays major."""
    m = _empty_shell()
    m["manifest_shape"] = "v7-arc"
    m["provenance"] = {"manifest_origin": "observational"}
    m["realized_files"] = [{"path": "memory/notes.md"}]
    m["scheduled_actions"] = [{
        "id": "daily-digest", "mechanism": "unknown", "install": {},
        "delivery_contract": {"mechanism": "message_send", "cadence": "daily"},
    }]
    findings = check_app_has_producer_surface(m, {})
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_MAJOR
    assert findings[0].evidence.get("declares_delivery_intent") is True


def test_v7_arc_observational_declaring_outputs_stays_major() -> None:
    """A non-empty top-level outputs[] is also a declared delivery
    obligation — observed app, data files present, but it claims a
    channel-kind output it can't produce without a surface."""
    m = _empty_shell()
    m["manifest_shape"] = "v7-arc"
    m["provenance"] = {"created_by": "scanner"}
    m["realized_files"] = [{"path": "memory/notes.md"}]
    m["outputs"] = [{"kind": "summary", "channel": "telegram"}]
    findings = check_app_has_producer_surface(m, {})
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_MAJOR


def test_v7_arc_observational_only_own_manifest_downgrades_to_info() -> None:
    """Daily-Logging-on-pod shape: observational Instance whose only
    realized file is its own manifest json. No attributed user data, so
    not a passive *data* store — but also not operator-actionable (the
    scanner inferred a name and bound no work). Downgrade to info, out of
    the alert lane, instead of the old major."""
    m = _empty_shell()
    m["manifest_shape"] = "v7-arc"
    m["provenance"] = {"manifest_origin": "observational", "created_by": "scanner"}
    m["instance_id"] = "app-daily-logging"
    m["realized_files"] = [{"path": "manifests/app-daily-logging.json"}]
    findings = check_app_has_producer_surface(m, {})
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_INFO
    assert findings[0].evidence.get("observational_shell") is True


def test_v7_arc_observational_registry_of_manifests_downgrades_to_info() -> None:
    """Manifest-Registry-on-pod shape: observational Instance whose
    realized files are all OTHER manifests (a scanner-minted registry
    artifact). No attributed user data → info, not major."""
    m = _empty_shell()
    m["manifest_shape"] = "v7-arc"
    m["provenance"] = {"created_by": "scanner"}
    m["realized_files"] = [
        {"path": "manifests/app-foo.json"},
        {"path": "manifests/app-bar.json"},
    ]
    findings = check_app_has_producer_surface(m, {})
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_INFO


def test_v7_arc_observational_with_script_does_not_fire_at_all() -> None:
    """Regression guard: a script in realized_files is still a producer
    surface (v23.2), so the check returns [] before the passive-app branch
    is even reached — script apps are never mislabeled passive."""
    m = _empty_shell()
    m["manifest_shape"] = "v7-arc"
    m["provenance"] = {"created_by": "scanner"}
    m["realized_files"] = [{"path": "tools/docx_generator.py"}]
    assert check_app_has_producer_surface(m, {}) == []


def test_non_observational_with_data_files_stays_major() -> None:
    """A DESIGNED app (forge/gallery/migration — not observational) with a
    data file but no producer surface is still operator-actionable: the
    author built an app and forgot to wire a trigger. The passive-app
    carve-out is scoped to observational instances only."""
    m = _empty_shell()
    m["manifest_shape"] = "v7-arc"
    m["provenance"] = {"manifest_origin": "forge", "installed_by": "forge"}
    m["realized_files"] = [{"path": "data/notes.md"}]
    findings = check_app_has_producer_surface(m, {})
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_MAJOR


def test_v7_arc_migrated_instance_stays_major() -> None:
    """Migration-derived v7-arc Instances (manifest_origin != observational)
    don't qualify for the scanner-shell downgrade — migrate_v7 produced
    them from operator-authored v13 data, so empty producer surfaces are
    real operator-actionable gaps, not a scanner attribution failure."""
    m = _empty_shell()
    m["manifest_shape"] = "v7-arc"
    m["provenance"] = {
        "installed_by": "migration",
        "spec_id": "p-deadbeef",
        "spec_version": "2026.05.20-1.0",
    }
    m["realized_files"] = []
    findings = check_app_has_producer_surface(m, {})
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_MAJOR


# ── check_v7_arc_instance_has_spec_binding (orphan_v7_arc_instance) ─────────


def _v7_arc_instance(**overrides) -> dict:
    base = {
        "instance_id": "i-test",
        "manifest_shape": "v7-arc",
        "schema_version": 14,
        "name": "Test",
        "status": "active",
        "provenance": {
            "spec_id": "p-deadbeef",
            "spec_version": "2026.05.20-1.0",
        },
    }
    base.update(overrides)
    return base


def test_orphan_check_fires_on_null_spec_id() -> None:
    """The team-bot-a case: v7-arc Instance with spec_id: None."""
    from app_audit_structural import check_v7_arc_instance_has_spec_binding
    m = _v7_arc_instance(provenance={"spec_id": None, "spec_version": None})
    findings = check_v7_arc_instance_has_spec_binding(m, {})
    assert len(findings) == 1
    assert findings[0].assertion_id == "orphan_v7_arc_instance"
    assert findings[0].severity == SEVERITY_MAJOR
    assert findings[0].evidence["spec_id"] is None


def test_orphan_check_fires_on_missing_spec_version() -> None:
    from app_audit_structural import check_v7_arc_instance_has_spec_binding
    m = _v7_arc_instance(provenance={"spec_id": "p-abc12345", "spec_version": None})
    findings = check_v7_arc_instance_has_spec_binding(m, {})
    assert len(findings) == 1
    assert findings[0].evidence["spec_version"] is None


def test_orphan_check_fires_on_missing_spec_file(tmp_path) -> None:
    """spec_id + spec_version both present, but Spec file doesn't exist on disk."""
    from app_audit_structural import check_v7_arc_instance_has_spec_binding
    m = _v7_arc_instance()
    # tmp_path has no gallery/ subtree → all candidate paths miss.
    findings = check_v7_arc_instance_has_spec_binding(m, {"shared_dir": tmp_path})
    assert len(findings) == 1
    assert "no Spec file exists" in findings[0].summary
    assert findings[0].evidence["spec_id"] == "p-deadbeef"
    assert len(findings[0].evidence["checked_paths"]) >= 2


def test_orphan_check_passes_when_local_spec_exists(tmp_path) -> None:
    from app_audit_structural import check_v7_arc_instance_has_spec_binding
    spec_dir = tmp_path / "gallery" / "local" / "p-deadbeef"
    spec_dir.mkdir(parents=True)
    (spec_dir / "2026.05.20-1.0.json").write_text("{}")
    m = _v7_arc_instance()
    findings = check_v7_arc_instance_has_spec_binding(m, {"shared_dir": tmp_path})
    assert findings == []


def test_orphan_check_passes_when_builtin_spec_exists(tmp_path) -> None:
    from app_audit_structural import check_v7_arc_instance_has_spec_binding
    spec_dir = tmp_path / "gallery" / "builtin" / "p-deadbeef"
    spec_dir.mkdir(parents=True)
    (spec_dir / "2026.05.20-1.0.json").write_text("{}")
    m = _v7_arc_instance()
    findings = check_v7_arc_instance_has_spec_binding(m, {"shared_dir": tmp_path})
    assert findings == []


def test_orphan_check_passes_when_imported_spec_exists(tmp_path) -> None:
    from app_audit_structural import check_v7_arc_instance_has_spec_binding
    spec_dir = tmp_path / "gallery" / "imported" / "pod-other" / "p-deadbeef"
    spec_dir.mkdir(parents=True)
    (spec_dir / "2026.05.20-1.0.json").write_text("{}")
    m = _v7_arc_instance(provenance={
        "spec_id": "p-deadbeef",
        "spec_version": "2026.05.20-1.0",
        "source_pod_id": "pod-other",
    })
    findings = check_v7_arc_instance_has_spec_binding(m, {"shared_dir": tmp_path})
    assert findings == []


def test_orphan_check_skipped_for_non_v7_arc() -> None:
    from app_audit_structural import check_v7_arc_instance_has_spec_binding
    m = {"manifest_shape": "v6", "id": "legacy"}
    assert check_v7_arc_instance_has_spec_binding(m, {}) == []


def test_orphan_check_skipped_for_retired_instance() -> None:
    from app_audit_structural import check_v7_arc_instance_has_spec_binding
    m = _v7_arc_instance(
        status="retired",
        provenance={"spec_id": None, "spec_version": None},
    )
    assert check_v7_arc_instance_has_spec_binding(m, {}) == []


def test_orphan_check_skips_disk_check_without_shared_dir() -> None:
    """Older runner contexts (or out-of-runner unit tests) may omit
    shared_dir. spec_id + spec_version present → pass without disk check."""
    from app_audit_structural import check_v7_arc_instance_has_spec_binding
    m = _v7_arc_instance()
    assert check_v7_arc_instance_has_spec_binding(m, {}) == []


def test_orphan_assertion_listed_in_default_assertions() -> None:
    """Regression guard."""
    from app_audit_structural import (
        DEFAULT_ASSERTIONS,
        check_v7_arc_instance_has_spec_binding,
    )
    assert check_v7_arc_instance_has_spec_binding in DEFAULT_ASSERTIONS


def test_orphan_assertion_id_listed_in_assertion_ids() -> None:
    from app_audit_structural import ASSERTION_IDS
    assert "orphan_v7_arc_instance" in ASSERTION_IDS


# ── v23.2: script realized_files as inferred CLI surface ────────────────────
#
# A script in ``realized_files[]`` IS an operator-invocable surface — the
# operator runs ``python scripts/foo.py`` to invoke it. v7-arc Instance
# migration dropped interface_contract but kept realized_files, leaving
# ~10–15 manifests on the 2026-06-09 pod survey firing
# app_no_producer_surface despite being runnable scripts. Counting the
# script as evidence closes that gap.


def test_has_script_realized_file_true_for_py_script() -> None:
    m = _empty_shell()
    m["realized_files"] = [
        {"logical_name": "build_vineyard_db",
         "path": "scripts/build_vineyard_db.py"},
    ]
    assert _has_script_realized_file(m) is True


def test_has_script_realized_file_true_for_sh_script() -> None:
    m = _empty_shell()
    m["realized_files"] = [{"path": "scripts/run.sh"}]
    assert _has_script_realized_file(m) is True


def test_has_script_realized_file_true_for_bash_script() -> None:
    m = _empty_shell()
    m["realized_files"] = [{"path": "scripts/run.bash"}]
    assert _has_script_realized_file(m) is True


def test_has_script_realized_file_false_for_data_files() -> None:
    """Data / doc / log files are NOT producer surfaces — only scripts count."""
    m = _empty_shell()
    m["realized_files"] = [
        {"path": "data/vineyard.db"},
        {"path": "docs/README.md"},
        {"path": "logs/run.jsonl"},
    ]
    assert _has_script_realized_file(m) is False


def test_has_script_realized_file_false_for_empty_realized_files() -> None:
    m = _empty_shell()
    m["realized_files"] = []
    assert _has_script_realized_file(m) is False


def test_has_script_realized_file_tolerates_non_dict_entries() -> None:
    """Malformed entries (strings, None) are silently skipped."""
    m = _empty_shell()
    m["realized_files"] = ["scripts/foo.py", None, {"path": "scripts/bar.py"}]
    assert _has_script_realized_file(m) is True


def test_surface_kinds_detects_script_realized_file() -> None:
    m = _empty_shell()
    m["realized_files"] = [{"path": "scripts/build_vineyard_db.py"}]
    assert "realized_files.script" in producer_surface_kinds(m)


def test_script_only_manifest_no_longer_fires() -> None:
    """The vineyard-ops / document-generator class: realized_files
    carries a Python script, interface_contract is empty, no
    heartbeat / cron / event. Pre-v23.2 this fired
    app_no_producer_surface; post-v23.2 the script IS the surface."""
    m = _empty_shell()
    m["realized_files"] = [
        {"logical_name": "build_vineyard_db",
         "path": "scripts/build_vineyard_db.py",
         "marker_state": "OWNED"},
    ]
    assert check_app_has_producer_surface(m, {}) == []


def test_script_with_uppercase_extension_still_counts() -> None:
    """Path extension check is case-insensitive (.PY, .Sh, etc.)."""
    m = _empty_shell()
    m["realized_files"] = [{"path": "scripts/Foo.PY"}]
    assert _has_script_realized_file(m) is True


def test_finding_evidence_lists_realized_files_script() -> None:
    """When the finding DOES fire, its fields_checked names the new
    field so operators know it was considered."""
    findings = check_app_has_producer_surface(_empty_shell(), {})
    assert len(findings) == 1
    assert "realized_files.script" in findings[0].evidence["fields_checked"]
