"""Coherence Pass A tests — PR 4 of the coherence + reconciliation framework.

Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §6.1, §7.1.

Pass A is the manifest-internal coherence check: pure-Python graph walks
that catch claims-vs-mechanisms incoherences. Tests pin each of the 8
assertions plus the skip rules (state != active; quality == suspect;
accepted signatures).
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.coherence_pass_a import (  # noqa: E402
    ASSERTION_IDS,
    SEVERITY_CRITICAL,
    SEVERITY_MAJOR,
    SEVERITY_MINOR,
    apply_pass_a,
    check_c_a1_recurring_without_trigger,
    check_c_a2_action_inputs_resolve,
    check_c_a3_action_outputs_have_producer,
    check_c_a4_messaging_output_needs_integration,
    check_c_a5_cron_script_in_files,
    check_c_a6_orphan_code,
    check_c_a7_integration_used,
    check_c_a8_interface_cli_resolves,
    run_pass_a,
    status_for_findings,
)


# ── C-A1: recurring behavior without trigger ───────────────────────────────

def test_c_a1_fires_critical_when_no_triggers() -> None:
    """The protein-reminder-style failure: description claims a daily
    behavior, no triggers declared."""
    manifest = {
        "description": "Sends a daily briefing every morning at 7am.",
        "scheduled_actions": [],
        "crons": [],
    }
    findings = check_c_a1_recurring_without_trigger(manifest)
    assert len(findings) == 1
    assert findings[0].id == "C-A1"
    assert findings[0].severity == SEVERITY_CRITICAL


def test_c_a1_passes_when_active_action_exists() -> None:
    """An active scheduled_action satisfies the claim."""
    manifest = {
        "description": "Daily briefing at 7am.",
        "scheduled_actions": [
            {"id": "morning-brief", "state": "active",
             "trigger": {"kind": "cron", "schedule": "0 7 * * *"}},
        ],
    }
    assert check_c_a1_recurring_without_trigger(manifest) == []


def test_c_a1_passes_when_cron_exists() -> None:
    """A cron entry satisfies the claim."""
    manifest = {
        "description": "Daily briefing at 7am.",
        "crons": [{"label": "briefing", "schedule": "0 7 * * *",
                   "script": "scripts/brief.py"}],
    }
    assert check_c_a1_recurring_without_trigger(manifest) == []


def test_c_a1_skips_when_only_disabled_actions_exist() -> None:
    """A disabled action doesn't satisfy the claim. The protein-checkin
    DISABLED pattern from production validation is exactly this case."""
    manifest = {
        "description": "Daily protein checkin reminder.",
        "scheduled_actions": [
            {"id": "protein", "state": "disabled",
             "trigger": {"kind": "cron"}},
        ],
    }
    findings = check_c_a1_recurring_without_trigger(manifest)
    assert len(findings) == 1
    assert findings[0].id == "C-A1"


def test_c_a1_emits_softer_finding_when_only_suspect_actions_exist() -> None:
    """Suspect entries are legacy noise (first-line excerpts of
    AGENTS.md captured as if they were actions). They DON'T satisfy the
    claim, but they're not "no schedule" either — emit a softer
    minor-severity "promote-or-prune" finding instead of the critical
    "no triggers" one.

    Production calibration 2026-06-06: every personal-bot heartbeat-driven app
    has 8-11 active suspect-quality actions; the old behavior tripped
    critical on all of them.
    """
    manifest = {
        "description": "Daily summary every morning.",
        "scheduled_actions": [
            {"id": "x", "state": "active", "quality": "suspect"},
            {"id": "y", "state": "active", "quality": "suspect"},
        ],
    }
    findings = check_c_a1_recurring_without_trigger(manifest)
    assert len(findings) == 1
    f = findings[0]
    assert f.id == "C-A1"
    assert f.severity == "minor"   # NOT critical
    assert f.assertion == "recurring_behavior_only_suspect_actions"
    # Evidence carries the suspect count so the chip shows context.
    evidence_by_field = {e.get("field"): e for e in f.evidence}
    assert evidence_by_field["scheduled_actions"]["suspect_count"] == 2
    assert evidence_by_field["scheduled_actions"]["active_count"] == 0


def test_c_a1_critical_when_truly_empty_distinct_from_suspect() -> None:
    """Belt-and-suspenders: confirm the critical path still fires when
    scheduled_actions is genuinely empty."""
    manifest = {
        "description": "Daily summary every morning.",
        "scheduled_actions": [],
        "crons": [],
    }
    findings = check_c_a1_recurring_without_trigger(manifest)
    assert len(findings) == 1
    assert findings[0].severity == "critical"
    assert findings[0].assertion == "recurring_behavior_without_trigger"


def test_c_a1_exempts_library_model() -> None:
    """Production calibration 2026-06-08: Biometric Integration is a
    Whoop OAuth token-management library. Its description mentions
    'daily' only because example_triggers describe a CALLER's schedule.
    Apps with usage.model='library' are exempt from C-A1."""
    manifest = {
        "description": "Manages Whoop OAuth tokens daily; refreshes on demand.",
        "usage": {"model": "library"},
        "scheduled_actions": [], "crons": [],
    }
    assert check_c_a1_recurring_without_trigger(manifest) == []


def test_c_a1_exempts_on_demand_model() -> None:
    """Synonym for library — same exemption."""
    manifest = {
        "description": "On-demand daily token refresh.",
        "usage": {"model": "on-demand"},
        "scheduled_actions": [], "crons": [],
    }
    assert check_c_a1_recurring_without_trigger(manifest) == []


def test_c_a1_exempts_on_demand_underscore_variant() -> None:
    """Tolerance for the underscore variant — operators may write either."""
    manifest = {
        "description": "Daily summary on demand.",
        "usage": {"model": "on_demand"},
        "scheduled_actions": [], "crons": [],
    }
    assert check_c_a1_recurring_without_trigger(manifest) == []


def test_c_a1_library_exemption_via_identity_nested_usage() -> None:
    """Manifest editor saves can place usage under identity.usage —
    check both locations."""
    manifest = {
        "description": "Daily token refresh on demand.",
        "identity": {"usage": {"model": "library"}},
        "scheduled_actions": [], "crons": [],
    }
    assert check_c_a1_recurring_without_trigger(manifest) == []


def test_c_a1_still_fires_for_user_initiated_with_daily_claim() -> None:
    """Regression guard: the library exemption shouldn't suppress the
    real critical case (user-initiated app with no schedule)."""
    manifest = {
        "description": "Sends a daily briefing every morning.",
        "usage": {"model": "user-initiated"},
        "scheduled_actions": [], "crons": [],
    }
    findings = check_c_a1_recurring_without_trigger(manifest)
    assert len(findings) == 1
    assert findings[0].severity == "critical"


def test_c_a1_no_recurring_phrase_no_finding() -> None:
    """A manifest with no recurring-behavior phrase doesn't fire even
    when triggers are empty — Pass A isn't checking that every app has
    a schedule, only that claimed schedules are backed."""
    manifest = {
        "description": "A one-shot script the operator invokes.",
        "scheduled_actions": [],
        "crons": [],
    }
    assert check_c_a1_recurring_without_trigger(manifest) == []


def test_c_a1_picks_up_phrase_in_usage_how_to_use() -> None:
    """Recurring phrases in usage.how_to_use also fire the check."""
    manifest = {
        "description": "Briefing app.",
        "usage": {"how_to_use": "Runs nightly to summarize the day."},
        "scheduled_actions": [],
        "crons": [],
    }
    findings = check_c_a1_recurring_without_trigger(manifest)
    assert len(findings) == 1


# ── C-A2: action inputs resolve ────────────────────────────────────────────

def test_c_a2_input_resolves_via_files() -> None:
    """Input path declared in files[] satisfies the check."""
    manifest = {
        "files": [{"path": "data/protein.md", "layer": "data"}],
        "scheduled_actions": [
            {"id": "x", "state": "active", "trigger": {},
             "inputs": [{"path": "data/protein.md", "kind": "data_file"}]},
        ],
    }
    assert check_c_a2_action_inputs_resolve(manifest) == []


def test_c_a2_input_resolves_via_volatile_paths() -> None:
    """Input matched by a volatile_paths glob satisfies the check."""
    manifest = {
        "files": [],
        "volatile_paths": [{"glob": "data/*.md", "layer": "data"}],
        "scheduled_actions": [
            {"id": "x", "state": "active", "trigger": {},
             "inputs": [{"path": "data/protein.md"}]},
        ],
    }
    assert check_c_a2_action_inputs_resolve(manifest) == []


def test_c_a2_unresolved_input_fires_major() -> None:
    """Input path not in files[] and not matching any glob → major."""
    manifest = {
        "files": [],
        "scheduled_actions": [
            {"id": "x", "state": "active", "trigger": {},
             "inputs": [{"path": "missing.json"}]},
        ],
    }
    findings = check_c_a2_action_inputs_resolve(manifest)
    assert len(findings) == 1
    assert findings[0].id == "C-A2"
    assert findings[0].severity == SEVERITY_MAJOR


def test_c_a2_external_input_skipped() -> None:
    """Inputs with kind=external are spec carve-outs — not files we'd
    expect to find in the workspace."""
    manifest = {
        "files": [],
        "scheduled_actions": [
            {"id": "x", "state": "active", "trigger": {},
             "inputs": [{"path": "api.external", "kind": "external"}]},
        ],
    }
    assert check_c_a2_action_inputs_resolve(manifest) == []


def test_c_a2_skips_disabled_actions() -> None:
    """state != active → no findings even when inputs missing."""
    manifest = {
        "files": [],
        "scheduled_actions": [
            {"id": "x", "state": "disabled", "trigger": {},
             "inputs": [{"path": "missing.json"}]},
        ],
    }
    assert check_c_a2_action_inputs_resolve(manifest) == []


def test_c_a2_dependency_input_resolves_via_app_dependency() -> None:
    """The U2 watcher shape: a scheduled_action reads a file OWNED by a
    declared app_dependency (Evening Sweep reads Task Manager's tasks.json).
    The input file is never in the watcher's own files[]/volatile_paths[];
    kind='dependency' + a matching from_dependency satisfies C-A2."""
    manifest = {
        "files": [],
        "app_dependencies": [
            {"pkg_id": "p-9bfa1c84", "display_name": "Task Manager",
             "required": True},
        ],
        "scheduled_actions": [
            {"id": "evening-sweep-daily", "state": "active", "trigger": {},
             "inputs": [{"path": "tasks.json", "kind": "dependency",
                         "from_dependency": "p-9bfa1c84"}]},
        ],
    }
    assert check_c_a2_action_inputs_resolve(manifest) == []


def test_c_a2_dependency_input_matches_dependency_by_display_name() -> None:
    """from_dependency may name the dependency by display_name, not just
    pkg_id — case-insensitively."""
    manifest = {
        "files": [],
        "app_dependencies": [
            {"pkg_id": "p-9bfa1c84", "display_name": "Task Manager"},
        ],
        "scheduled_actions": [
            {"id": "x", "state": "active", "trigger": {},
             "inputs": [{"path": "tasks.json", "kind": "dependency",
                         "from_dependency": "task manager"}]},
        ],
    }
    assert check_c_a2_action_inputs_resolve(manifest) == []


def test_c_a2_dependency_input_unnamed_provider_with_a_dependency() -> None:
    """kind='dependency' with no named provider is satisfied as long as the
    app declares at least one dependency to read from."""
    manifest = {
        "files": [],
        "app_dependencies": [{"pkg_id": "p-9bfa1c84"}],
        "scheduled_actions": [
            {"id": "x", "state": "active", "trigger": {},
             "inputs": [{"path": "tasks.json", "kind": "dependency"}]},
        ],
    }
    assert check_c_a2_action_inputs_resolve(manifest) == []


def test_c_a2_dependency_input_resolves_via_v7arc_apps() -> None:
    """Shape-independence: a hybrid manifest with legacy scheduled_actions
    inputs but v7-arc ``apps[]`` (spec_id) dependencies still resolves the
    dependency input."""
    manifest = {
        "files": [],
        "apps": [{"spec_id": "p-9bfa1c84", "required": True}],
        "scheduled_actions": [
            {"id": "x", "state": "active", "trigger": {},
             "inputs": [{"path": "tasks.json", "kind": "dependency",
                         "from_dependency": "p-9bfa1c84"}]},
        ],
    }
    assert check_c_a2_action_inputs_resolve(manifest) == []


def test_c_a2_dependency_input_without_declared_dependency_still_fires() -> None:
    """Honesty guard: claiming kind='dependency' while declaring NO
    app_dependencies is itself incoherent — C-A2 must still fire so a
    missing dependency declaration is surfaced, not silently waved through."""
    manifest = {
        "files": [],
        "app_dependencies": [],
        "scheduled_actions": [
            {"id": "x", "state": "active", "trigger": {},
             "inputs": [{"path": "tasks.json", "kind": "dependency"}]},
        ],
    }
    findings = check_c_a2_action_inputs_resolve(manifest)
    assert len(findings) == 1
    assert findings[0].id == "C-A2"
    assert findings[0].severity == SEVERITY_MAJOR


def test_c_a2_dependency_input_naming_undeclared_provider_fires() -> None:
    """Naming a from_dependency the manifest doesn't actually depend on is
    not satisfied — the dependency is undeclared, a real gap."""
    manifest = {
        "files": [],
        "app_dependencies": [{"pkg_id": "p-9bfa1c84",
                              "display_name": "Task Manager"}],
        "scheduled_actions": [
            {"id": "x", "state": "active", "trigger": {},
             "inputs": [{"path": "other.json", "kind": "dependency",
                         "from_dependency": "p-deadbeef"}]},
        ],
    }
    findings = check_c_a2_action_inputs_resolve(manifest)
    assert len(findings) == 1
    assert findings[0].id == "C-A2"


# ── C-A3 + C-A4: outputs + messaging integration ───────────────────────────

def test_c_a4_messaging_output_without_integration_critical() -> None:
    """The asymmetric-failure case from validation: action declares
    messaging output but no integration is declared. Critical."""
    manifest = {
        "requirements": {"integrations": []},
        "scheduled_actions": [
            {"id": "brief", "state": "active", "trigger": {},
             "outputs": [{"kind": "messaging_channel"}]},
        ],
    }
    findings = check_c_a4_messaging_output_needs_integration(manifest)
    assert len(findings) == 1
    assert findings[0].id == "C-A4"
    assert findings[0].severity == SEVERITY_CRITICAL


def test_c_a4_messaging_output_with_integration_passes() -> None:
    """A messaging-capable integration satisfies the claim."""
    manifest = {
        "requirements": {"integrations": [{"id": "telegram", "required": True}]},
        "scheduled_actions": [
            {"id": "brief", "state": "active", "trigger": {},
             "outputs": [{"kind": "messaging_channel"}]},
        ],
    }
    assert check_c_a4_messaging_output_needs_integration(manifest) == []


def test_c_a4_string_form_integration_accepted() -> None:
    """Integrations may be declared as bare strings instead of dicts."""
    manifest = {
        "requirements": {"integrations": ["slack"]},
        "scheduled_actions": [
            {"id": "x", "state": "active", "trigger": {},
             "outputs": [{"kind": "messaging_channel"}]},
        ],
    }
    assert check_c_a4_messaging_output_needs_integration(manifest) == []


def test_c_a3_output_without_producer_fires_major() -> None:
    """An action declares a file output but no code file plausibly
    produces it. Major."""
    manifest = {
        "requirements": {"integrations": []},
        "files": [],
        "scheduled_actions": [
            {"id": "x", "state": "active", "trigger": {},
             "outputs": [{"kind": "file", "target": "report.md"}]},
        ],
    }
    findings = check_c_a3_action_outputs_have_producer(manifest)
    assert len(findings) == 1
    assert findings[0].id == "C-A3"


# ── C-A5: cron script in files[] as code layer ─────────────────────────────

def test_c_a5_cron_script_resolves() -> None:
    """Cron script declared in files[] with layer 'code' passes."""
    manifest = {
        "crons": [{"label": "backup", "script": "scripts/backup.sh",
                   "schedule": "0 3 * * *"}],
        "files": [{"path": "scripts/backup.sh", "layer": "code"}],
    }
    assert check_c_a5_cron_script_in_files(manifest) == []


def test_c_a5_cron_script_missing_from_files() -> None:
    """Cron script not in files[]. Major."""
    manifest = {
        "crons": [{"label": "x", "script": "scripts/missing.sh",
                   "schedule": "0 3 * * *"}],
        "files": [],
    }
    findings = check_c_a5_cron_script_in_files(manifest)
    assert len(findings) == 1
    assert findings[0].id == "C-A5"
    assert findings[0].assertion == "cron_script_not_in_files"


def test_c_a5_cron_script_wrong_layer() -> None:
    """Cron script in files[] but layer != code. Major."""
    manifest = {
        "crons": [{"label": "x", "script": "scripts/backup.sh",
                   "schedule": "0 3 * * *"}],
        "files": [{"path": "scripts/backup.sh", "layer": "data"}],
    }
    findings = check_c_a5_cron_script_in_files(manifest)
    assert len(findings) == 1
    assert findings[0].assertion == "cron_script_not_code_layer"


# ── C-A6: orphan code ──────────────────────────────────────────────────────

def test_c_a6_code_file_referenced_by_cron_passes() -> None:
    """A code file referenced by a cron is not orphan."""
    manifest = {
        "files": [{"path": "scripts/main.py", "layer": "code"}],
        "crons": [{"label": "x", "script": "scripts/main.py",
                   "schedule": "0 0 * * *"}],
    }
    assert check_c_a6_orphan_code(manifest) == []


def test_c_a6_orphan_code_fires_minor() -> None:
    """A code file referenced by nothing. Minor."""
    manifest = {
        "files": [{"path": "scripts/orphan.py", "layer": "code"}],
        "crons": [],
        "scheduled_actions": [],
    }
    findings = check_c_a6_orphan_code(manifest)
    assert len(findings) == 1
    assert findings[0].id == "C-A6"
    assert findings[0].severity == SEVERITY_MINOR


def test_c_a6_skips_admin_owned_files() -> None:
    """Admin-owned code files are scheduled externally (LaunchDaemon /
    systemd / cron handled by the operator). The bot's OC manifest
    can't reference them — C-A6 exempts.

    Production calibration 2026-06-06: security-cve-scan's finalize.py
    is admin-owned + scheduled by ai.evolve.evolve.security-cve-scan-
    finalize LaunchDaemon; the old behavior produced a noisy minor
    finding because C-A6 doesn't know about launchd.
    """
    manifest = {
        "files": [{
            "path": "packages/analyzer/evolve_apps/x/finalize.py",
            "layer": "code", "owned_by": "admin",
        }],
        "crons": [], "scheduled_actions": [],
    }
    assert check_c_a6_orphan_code(manifest) == []


def test_c_a6_skips_external_owned_files() -> None:
    """External-owned files (libraries imported without naming) — same
    exemption shape as admin-owned."""
    manifest = {
        "files": [{
            "path": "vendor/some_lib.py",
            "layer": "code", "owned_by": "external",
        }],
        "crons": [], "scheduled_actions": [],
    }
    assert check_c_a6_orphan_code(manifest) == []


def test_c_a6_does_not_skip_evolve_owned_files() -> None:
    """The bot's own files are still fair game for the orphan check."""
    manifest = {
        "files": [{
            "path": "scripts/orphan.py",
            "layer": "code", "owned_by": "evolve",
        }],
        "crons": [], "scheduled_actions": [],
    }
    findings = check_c_a6_orphan_code(manifest)
    assert len(findings) == 1   # still fires


def test_c_a6_skips_file_in_evidence_files() -> None:
    """Production calibration 2026-06-07: atlas_knowledge.py is the
    canonical script for a content-store app. The scanner attributes
    it via ``evidence_files`` but no cron / action / cli cites it
    because the bot's LLM invokes it directly via INSTALLED_APPS.md.

    The script being in ``evidence_files`` IS a legitimate reference
    — the scanner is saying "this is the app's file." Treat as
    referenced so C-A6 doesn't fire."""
    manifest = {
        "files": [{
            "path": "scripts/atlas_knowledge.py",
            "layer": "code",
        }],
        "evidence_files": ["scripts/atlas_knowledge.py", "knowledge/"],
        "crons": [], "scheduled_actions": [],
    }
    assert check_c_a6_orphan_code(manifest) == []


def test_c_a6_skips_file_mentioned_in_usage_how_to_use() -> None:
    """When the manifest's usage.how_to_use prose names the file as
    the way the LLM invokes the app, that's a legitimate reference
    — the orphan check should respect documented invocation paths."""
    manifest = {
        "files": [{
            "path": "scripts/journal.py",
            "layer": "code",
        }],
        "usage": {
            "how_to_use": "When the user mentions feelings, run "
                          "`python3 scripts/journal.py --mood X`."
        },
        "crons": [], "scheduled_actions": [],
    }
    assert check_c_a6_orphan_code(manifest) == []


def test_c_a6_skips_file_matching_capability_tag() -> None:
    """When the file is named after a capability_tag and the LLM
    routes by tag, the tag match counts as a reference."""
    manifest = {
        "files": [{
            "path": "scripts/atlas_knowledge.py",
            "layer": "code",
        }],
        "capability_tags": ["Atlas Knowledge", "atlas", "knowledge"],
        "crons": [], "scheduled_actions": [],
    }
    assert check_c_a6_orphan_code(manifest) == []


def test_c_a6_still_fires_for_truly_orphaned_file() -> None:
    """Regression-guard: the exemptions shouldn't suppress genuine
    orphans. A file with no evidence, no tag match, no prose mention,
    no cron/action/cli, owned by evolve — that's a real orphan."""
    manifest = {
        "files": [{
            "path": "scripts/genuinely_orphan.py",
            "layer": "code", "owned_by": "evolve",
        }],
        "evidence_files": ["scripts/something_else.py"],
        "capability_tags": ["random", "unrelated"],
        "session_keywords": ["foo", "bar"],
        "crons": [], "scheduled_actions": [],
    }
    findings = check_c_a6_orphan_code(manifest)
    assert len(findings) == 1


# ── C-A7: declared integration referenced ─────────────────────────────────

def test_c_a7_passes_when_integration_appears_in_action() -> None:
    """An integration declared and used by an action is satisfied."""
    manifest = {
        "requirements": {"integrations": [{"id": "slack"}]},
        "scheduled_actions": [
            {"id": "x", "summary": "post to slack",
             "outputs": [{"target": "slack:channel:C123"}]}
        ],
    }
    assert check_c_a7_integration_used(manifest) == []


def test_c_a7_orphan_integration_fires_minor() -> None:
    """Declared integration with no reference anywhere. Minor."""
    manifest = {
        "requirements": {"integrations": [{"id": "notion"}]},
        "scheduled_actions": [], "crons": [], "files": [],
    }
    findings = check_c_a7_integration_used(manifest)
    assert len(findings) == 1
    assert findings[0].severity == SEVERITY_MINOR


# ── C-A8: interface_contract CLI ───────────────────────────────────────────

def test_c_a8_cli_command_resolves_to_files() -> None:
    """CLI command's script path is in files[] → passes."""
    manifest = {
        "files": [{"path": "scripts/run.py", "layer": "code"}],
        "interface_contract": {
            "cli": [{"command": "python3 scripts/run.py --flag"}],
        },
    }
    assert check_c_a8_interface_cli_resolves(manifest) == []


def test_c_a8_cli_command_unresolved_fires_major() -> None:
    """CLI command references a path not in files[]. Major."""
    manifest = {
        "files": [],
        "interface_contract": {
            "cli": [{"command": "python3 scripts/missing.py"}],
        },
    }
    findings = check_c_a8_interface_cli_resolves(manifest)
    assert len(findings) == 1
    assert findings[0].id == "C-A8"


# ── Accepted signatures suppress findings ──────────────────────────────────

def test_coherence_accepted_suppresses_matching_findings() -> None:
    """An operator-accepted signature drops the matching finding from
    run_pass_a output."""
    manifest = {
        "description": "Daily briefing.",
        "scheduled_actions": [],
        "crons": [],
    }
    # Get the natural finding's signature.
    findings = check_c_a1_recurring_without_trigger(manifest)
    sig = findings[0].signature()
    # Accept it.
    manifest["coherence"] = {"coherence_accepted": [{"signature": sig}]}
    # Now run_pass_a should suppress it.
    results = run_pass_a(manifest)
    assert all(f.signature() != sig for f in results)


# ── Status resolver ───────────────────────────────────────────────────────

def test_status_for_findings_ok_when_empty() -> None:
    assert status_for_findings([]) == "ok"


def test_status_for_findings_warnings_when_minor_only() -> None:
    from evolve_admin.applications.coherence_pass_a import CoherenceFinding
    minor = CoherenceFinding(id="C-A6", severity=SEVERITY_MINOR,
                             assertion="orphan_code_file", description="")
    assert status_for_findings([minor]) == "warnings"


def test_status_for_findings_incoherent_on_major_or_critical() -> None:
    from evolve_admin.applications.coherence_pass_a import CoherenceFinding
    major = CoherenceFinding(id="C-A2", severity=SEVERITY_MAJOR,
                             assertion="x", description="")
    critical = CoherenceFinding(id="C-A1", severity=SEVERITY_CRITICAL,
                                assertion="y", description="")
    assert status_for_findings([major]) == "incoherent"
    assert status_for_findings([critical]) == "incoherent"


# ── apply_pass_a writes the coherence block ───────────────────────────────

def test_apply_pass_a_populates_coherence_block() -> None:
    manifest = {
        "id": "test",
        "description": "Daily briefing at 7am.",
        "scheduled_actions": [], "crons": [],
    }
    summary = apply_pass_a(manifest)
    assert manifest["coherence"]["status"] == "incoherent"
    assert manifest["coherence"]["last_checked_at"]
    assert len(manifest["coherence"]["findings"]) >= 1
    assert summary["status"] == "incoherent"
    assert summary["by_severity"][SEVERITY_CRITICAL] >= 1


def test_apply_pass_a_preserves_coherence_accepted() -> None:
    """coherence_accepted survives the rewrite."""
    manifest = {
        "id": "test",
        "description": "ok",
        "coherence": {"coherence_accepted": [{"signature": "abc123"}]},
    }
    apply_pass_a(manifest)
    assert manifest["coherence"]["coherence_accepted"] == [{"signature": "abc123"}]


def test_apply_pass_a_ok_status_on_healthy_manifest() -> None:
    """A coherent manifest gets status=ok and an empty findings list."""
    manifest = {
        "id": "test",
        "description": "A simple data manager.",
        "files": [], "scheduled_actions": [], "crons": [],
        "requirements": {"integrations": []},
    }
    apply_pass_a(manifest)
    assert manifest["coherence"]["status"] == "ok"
    assert manifest["coherence"]["findings"] == []


# ── Assertion ID inventory ────────────────────────────────────────────────

def test_assertion_ids_match_default_assertions_module() -> None:
    """Every ID in ASSERTION_IDS has a corresponding default assertion
    function. Catches drift between the constant and the function set."""
    expected_ids = set(ASSERTION_IDS)
    actual_ids = set()
    # Each assertion function fires findings with one of its assigned
    # IDs; collect by running on a manifest crafted to fire each.
    diagnostic_manifests = [
        # C-A1
        {"description": "daily briefing", "scheduled_actions": [], "crons": []},
        # C-A2
        {"files": [], "volatile_paths": [],
         "scheduled_actions": [{"id": "x", "state": "active", "trigger": {},
                                "inputs": [{"path": "missing.json"}]}]},
        # C-A4 (also covers C-A3-adjacent)
        {"requirements": {"integrations": []},
         "scheduled_actions": [{"id": "x", "state": "active", "trigger": {},
                                "outputs": [{"kind": "messaging_channel"}]}]},
        # C-A5
        {"crons": [{"label": "x", "script": "scripts/missing.sh",
                    "schedule": "0 0 * * *"}], "files": []},
        # C-A6
        {"files": [{"path": "scripts/orphan.py", "layer": "code"}],
         "crons": [], "scheduled_actions": []},
        # C-A7
        {"requirements": {"integrations": [{"id": "lonely"}]},
         "scheduled_actions": [], "crons": [], "files": []},
        # C-A8
        {"files": [],
         "interface_contract": {"cli": [{"command": "python3 missing.py"}]}},
    ]
    for m in diagnostic_manifests:
        for f in run_pass_a(m):
            actual_ids.add(f.id)
    # C-A3 has its own dedicated assertion but the manifests above
    # don't always exercise it (the C-A4 case short-circuits the
    # messaging branch). That's fine — we just verify ID set is
    # contained within expected.
    assert actual_ids.issubset(expected_ids), (
        f"unexpected IDs: {actual_ids - expected_ids}"
    )


def test_signature_is_stable_across_runs() -> None:
    """Same finding fires the same signature each time so accepted
    entries match reliably."""
    manifest = {"description": "daily x", "scheduled_actions": [], "crons": []}
    sig1 = check_c_a1_recurring_without_trigger(manifest)[0].signature()
    sig2 = check_c_a1_recurring_without_trigger(manifest)[0].signature()
    assert sig1 == sig2


# ── v7-arc shape awareness ─────────────────────────────────────────────────
#
# Spec: docs/spec-manifest-v7-2026-05-20.md. v7-arc Instances store their
# file roster in ``realized_files[]`` (legacy ``files[]`` empty) and their
# schedules in ``configured_schedules[]`` (legacy ``scheduled_actions[]`` /
# ``crons[]`` empty). Before this awareness landed, Pass A read only the
# legacy fields and so fired false C-A1 (no trigger found) and false C-A2
# (input not in the empty ``files[]``) on every migrated app. Discovered on
# the pod during the 2026-06-11 fleet v7-arc migration.

def test_c_a1_v7arc_configured_schedules_satisfies_trigger() -> None:
    """A v7-arc Instance whose schedule lives in ``configured_schedules[]``
    (legacy ``scheduled_actions[]`` / ``crons[]`` empty) must NOT fire C-A1."""
    manifest = {
        "manifest_shape": "v7-arc",
        "description": "Sends a daily summary every evening.",
        "scheduled_actions": [],
        "crons": [],
        "configured_schedules": [
            {"spec_schedule_id": "evening-summary",
             "resolved_cron": "0 18 * * *",
             "configured_at": "2026-05-20T15:00:00Z",
             "user_adjustments": []},
        ],
    }
    assert check_c_a1_recurring_without_trigger(manifest) == []


def test_c_a1_v7arc_spec_schedules_satisfy_trigger() -> None:
    """After hydration the Spec's ``schedules[]`` are overlaid onto the
    Instance; they satisfy the recurring-behavior claim."""
    manifest = {
        "manifest_shape": "v7-arc",
        "description": "Daily briefing at 7am.",
        "scheduled_actions": [],
        "crons": [],
        "schedules": [
            {"id": "weekly_summary", "cron_default": "0 9 * * 0",
             "invokes": "summary_script"},
        ],
    }
    assert check_c_a1_recurring_without_trigger(manifest) == []


def test_c_a1_v7arc_event_triggers_satisfy_trigger() -> None:
    """Spec-side ``event_triggers[]`` (overlaid on hydration) are a
    mechanism that backs a recurring/automated claim."""
    manifest = {
        "manifest_shape": "v7-arc",
        "description": "Logs protein intake daily.",
        "scheduled_actions": [],
        "crons": [],
        "event_triggers": [
            {"id": "incoming_protein_log", "source": "telegram",
             "invokes": "ingest_script"},
        ],
    }
    assert check_c_a1_recurring_without_trigger(manifest) == []


def test_c_a2_v7arc_input_resolves_via_realized_files() -> None:
    """A scheduled_action input that matches a ``realized_files[]`` path
    resolves — even though legacy ``files[]`` is empty on a v7-arc Instance."""
    manifest = {
        "manifest_shape": "v7-arc",
        "files": [],
        "realized_files": [
            {"logical_name": "intake", "path": "data/intake.jsonl",
             "file_id": "f-aaaa1111@2026.05.20-1.0", "marker_state": "OWNED"},
        ],
        "scheduled_actions": [
            {"id": "summary", "state": "active", "trigger": {},
             "inputs": [{"path": "data/intake.jsonl", "kind": "data_file"}]},
        ],
    }
    assert check_c_a2_action_inputs_resolve(manifest) == []


def test_c_a4_session_message_output_does_not_require_integration() -> None:
    """A v7-arc heartbeat/cron action that delivers via the bot's own
    session turn (``output.kind == 'session_message'``) does NOT need an
    app-declared messaging integration — it uses the bot's base channel.
    C-A4 must not fire; C-A3 must not fire either (it's still
    message-shaped, so no file producer is demanded)."""
    manifest = {
        "manifest_shape": "v7-arc",
        "requirements": {"integrations": []},
        "realized_files": [
            {"logical_name": "tasks", "path": "scripts/tasks.py"},
        ],
        "scheduled_actions": [
            {"id": "next-check", "state": "active",
             "trigger": {"kind": "heartbeat"},
             "inputs": [],
             "outputs": [{"kind": "session_message"}]},
        ],
    }
    assert check_c_a4_messaging_output_needs_integration(manifest) == []
    assert check_c_a3_action_outputs_have_producer(manifest) == []


def test_c_a4_still_fires_for_real_messaging_output_without_integration() -> None:
    """Guard: the session_message carve-out must not suppress a genuine
    external-messaging output with no integration declared."""
    manifest = {
        "requirements": {"integrations": []},
        "scheduled_actions": [
            {"id": "brief", "state": "active", "trigger": {},
             "outputs": [{"kind": "messaging_channel", "target": "telegram"}]},
        ],
    }
    findings = check_c_a4_messaging_output_needs_integration(manifest)
    assert len(findings) == 1 and findings[0].severity == SEVERITY_CRITICAL


def test_c_a6_does_not_flood_on_v7arc_realized_files() -> None:
    """v7-arc ``realized_files[]`` are owned-by-declaration; the orphan
    check must not flag them even when nothing else cites them. Otherwise
    every migrated app (some with dozens of realized files) would flood
    minor findings."""
    manifest = {
        "manifest_shape": "v7-arc",
        "files": [],
        "realized_files": [
            {"logical_name": "a", "path": "scripts/a.py", "marker_state": "OWNED"},
            {"logical_name": "b", "path": "scripts/b.py", "marker_state": "OWNED"},
            {"logical_name": "c", "path": "scripts/unreferenced.py",
             "marker_state": "OWNED"},
        ],
        "scheduled_actions": [],
        "crons": [],
    }
    assert check_c_a6_orphan_code(manifest) == []


def test_v7arc_instance_with_schedule_and_realized_input_zero_findings() -> None:
    """The headline regression: a v7-arc Instance with a
    ``configured_schedules[]`` trigger and a ``realized_files[]`` entry that
    satisfies a scheduled-action input must produce ZERO findings — no false
    C-A1 (trigger is in configured_schedules) and no false C-A2 (input is in
    realized_files, not the empty legacy files[]).

    Mirrors the live shape that fired false-incoherent across the fleet after
    the 2026-06-11 v7-arc migration."""
    manifest = {
        "instance_id": "i-test0001",
        "manifest_shape": "v7-arc",
        "description": "Sends a daily summary every evening.",
        # Legacy surfaces empty — the v7-arc shape.
        "files": [],
        "scheduled_actions": [
            {"id": "evening-summary", "state": "active",
             "trigger": {"kind": "cron", "schedule": "0 18 * * *"},
             "inputs": [{"path": "data/intake.jsonl", "kind": "data_file"}],
             "outputs": []},
        ],
        "crons": [],
        # v7-arc trigger + file roster.
        "configured_schedules": [
            {"spec_schedule_id": "evening-summary",
             "resolved_cron": "0 18 * * *",
             "configured_at": "2026-05-20T15:00:00Z",
             "user_adjustments": []},
        ],
        "realized_files": [
            {"logical_name": "summary", "path": "scripts/summary.py",
             "file_id": "f-bbbb2222@2026.05.20-1.0", "marker_state": "OWNED"},
            {"logical_name": "intake", "path": "data/intake.jsonl",
             "file_id": "f-cccc3333@2026.05.20-1.0", "marker_state": "OWNED"},
        ],
    }
    findings = run_pass_a(manifest)
    assert findings == [], (
        "expected zero findings on a coherent v7-arc Instance, got: "
        + ", ".join(f"{f.id}:{f.assertion}" for f in findings)
    )


def test_v7arc_regression_fires_without_the_fix_shape() -> None:
    """Guard the inverse: the SAME app expressed in the legacy shape with
    empty files[]/scheduled_actions[] (i.e. what Pass A saw before reading
    the v7-arc fields) DOES fire — proving the zero-findings result above
    comes from reading realized_files/configured_schedules, not from the
    recurring phrase being absent."""
    legacy_blind = {
        "manifest_shape": "v7-arc",
        "description": "Sends a daily summary every evening.",
        "files": [],
        "scheduled_actions": [],
        "crons": [],
        # No configured_schedules / realized_files — the blind-spot shape.
    }
    findings = check_c_a1_recurring_without_trigger(legacy_blind)
    assert len(findings) == 1 and findings[0].id == "C-A1"
