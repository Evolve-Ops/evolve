#!/usr/bin/env python3
"""
app_audit_structural.py — Tier 2 structural assertions for app manifests.

Pure-Python reality checks against a manifest's claims. No LLM, no network,
no admin-server roundtrip. Imported by ``app_audit_runner.py`` (bot-side
worker) and reusable from forge-time verification on the admin side.

Each assertion returns a list of ``Finding`` dicts (zero findings = passing).
The runner aggregates findings across all assertions for an app, then emits
them as outbox records that the admin's audit poller ingests into the Signal
store.

This module contains only the assertion functions and supporting types.
Scheduling, I/O paths, and Signal-store integration live in their callers.

See ``docs/spec-app-audit-2026-05-16.md §3.1`` for the assertion catalog.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


# ── Severity + finding shape ─────────────────────────────────────────────────
#
# Severity values match the rest of the spec's audit vocabulary. Map to Signal
# severities at ingest time: critical → alert, major → warn, minor/info → info.
SEVERITY_CRITICAL = "critical"
SEVERITY_MAJOR    = "major"
SEVERITY_MINOR    = "minor"
SEVERITY_INFO     = "info"


# Assertions whose finding describes a BOT-LEVEL artifact (a LaunchAgent plist
# or a managed HEARTBEAT.md/AGENTS.md section) rather than a specific app. The
# orphan-install check runs once per manifest — the runner hands it the
# pod-wide claimed-artifact union — so a single orphan plist is re-discovered
# by every app's audit. Keying its signature on app_id therefore fans one
# orphan out into one Signal per app on the bot (18 and 13 across two bots in
# the 2026-06-12 review). These signatures omit app_id and key on the artifact
# identity instead, so every app's copy of the same orphan collapses to ONE
# Signal. See ``Finding.signature``.
_BOT_SCOPED_ASSERTIONS = frozenset({
    "scheduled_action_orphan_install",
})


@dataclass
class Finding:
    """One structural-verification finding against a manifest claim."""
    assertion_id: str        # stable identifier from ASSERTION_IDS below
    severity: str            # one of SEVERITY_* constants
    summary: str             # one-line human-readable
    evidence: dict[str, Any] = field(default_factory=dict)

    def signature(self, bot_id: str, app_id: str) -> str:
        """Stable signature for Signal dedup.

        Includes assertion_id + the load-bearing evidence keys so the same
        finding on the same target dedupes across runs, but different file
        paths or cron lines get distinct signatures.

        Bot-scoped assertions (``_BOT_SCOPED_ASSERTIONS``) describe a
        bot-level artifact, not an app, so their signature omits app_id and
        keys on the artifact identity (plist path/label, or ``file#anchor``).
        Without this, the orphan-install check — which the runner invokes once
        per manifest with the pod-wide claimed union — emits one Signal per app
        for a single orphan plist instead of one Signal for the plist.
        """
        if self.assertion_id in _BOT_SCOPED_ASSERTIONS:
            ev_key = ""
            for key in ("plist_path", "label", "artifact", "path"):
                v = self.evidence.get(key)
                if v:
                    ev_key = f"{key}={v}"
                    break
            return f"app_structural_verifier:{self.assertion_id}:{bot_id}:{ev_key}"
        ev_key = ""
        for key in ("path", "script", "schedule", "command", "package", "integration"):
            v = self.evidence.get(key)
            if v:
                ev_key = f"{key}={v}"
                break
        return f"app_structural_verifier:{self.assertion_id}:{bot_id}:{app_id}:{ev_key}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Observational provenance gate (single source of truth) ──────────────────
#
# Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §8.1.
#
# Each structural assertion targets a top-level manifest field. When that
# field's provenance is *observational* (the scanner inferred it; nobody
# authored it), a finding against it is trail-only — recorded in the per-app
# audit trail but never surfaced as a Signal. The operator can't action an
# observation the system itself synthesized.
#
# This predicate is the SINGLE SOURCE OF TRUTH for that decision. Two callers
# import it and they MUST NOT drift:
#   - the bot-side runner (``app_audit_runner._write_outbox_record`` path):
#     skips the outbox write entirely, so an observational finding is never
#     even shipped across the wire (the pre-2026-06-28 behavior wrote + shipped
#     the record and the admin dropped it on arrival — pure waste).
#   - the admin-side poller (``audit_poller._is_observational_finding``):
#     keeps the gate as a backstop for findings from older runners that still
#     ship observational records.
#
# Unknown assertion_ids fall through to "emit" — the safe default that keeps a
# newly-added assertion from being accidentally muted before its field is
# mapped here.
ASSERTION_TO_FIELD: dict[str, str] = {
    # Files-related assertions target the ``files`` field.
    "file_missing":                            "files",
    "file_sha_mismatch":                       "files",
    # Cron-related assertions target ``crons``.
    "cron_script_missing":                     "crons",
    "cron_schedule_unparseable":               "crons",
    "cron_not_in_crontab":                     "crons",
    # Openclaw cron status (Q32 assertions) — same field.
    "openclaw_cron_error":                     "crons",
    "openclaw_cron_skipped":                   "crons",
    "openclaw_cron_delivery_failure":          "crons",
    # Scheduled-action assertions target ``scheduled_actions``.
    "scheduled_action_evidence_path":          "scheduled_actions",
    "scheduled_action_anchor":                 "scheduled_actions",
    "scheduled_action_input_missing":          "scheduled_actions",
    "scheduled_action_install_missing":        "scheduled_actions",
    "scheduled_action_command_unresolvable":   "scheduled_actions",
    "scheduled_action_output_channel_invalid": "scheduled_actions",
    "scheduled_action_orphan_install":         "scheduled_actions",
    "scheduled_action_section_drift":          "scheduled_actions",
    "heartbeat_anchors_present":               "heartbeat_evidence",
    "cron_labels_loaded":                      "cron_evidence",
    # test_command and python_packages are independent fields.
    "test_command_unresolvable":               "test_command",
    "python_package_import_failed":            "requirements",
}

# The four authored provenance sources. A finding against an authored field
# fires a Signal; observational fields stay trail-only.
AUTHORED_PROVENANCE = frozenset({
    "forge_built", "user_authored", "bot_authored", "confirmed",
})


def is_observational_finding(assertion_id: str, provenance: dict | None) -> bool:
    """True iff ``assertion_id`` targets a field whose provenance is
    explicitly observational on the manifest.

    ``provenance`` is the manifest's ``provenance`` mapping (the runner passes
    ``manifest.get("provenance")``; the poller passes the loaded manifest's
    ``provenance`` attribute). Conservative — returns False (→ emit) for every
    ambiguous case:
      - unknown assertion_id (field unmapped) → emit
      - no provenance block / malformed shape → emit
      - field absent from ``field_origins`` → emit
      - source empty / unknown / a typo → emit

    Only an explicit ``source == "observational"`` mutes. This matches the
    pre-extraction admin-side behavior: existing manifests with empty
    provenance keep emitting until the write paths populate provenance.
    """
    field = ASSERTION_TO_FIELD.get(assertion_id or "")
    if not field:
        return False
    if not isinstance(provenance, dict):
        return False
    field_origins = provenance.get("field_origins")
    if not isinstance(field_origins, dict):
        return False
    entry = field_origins.get(field)
    if not isinstance(entry, dict):
        return False
    source = (entry.get("source") or "").strip().lower()
    return source == "observational"


# Stable IDs surfaced in signatures + outbox records. Keep additions backward-
# compatible: appending new IDs is fine; renaming an existing one breaks the
# Signal-store dedup so existing findings would re-fire as new.
ASSERTION_IDS = (
    "file_missing",
    "file_sha_mismatch",
    "cron_script_missing",
    "cron_schedule_unparseable",
    "cron_not_in_crontab",
    # "test_command_unresolvable" removed 2026-06-08 — app-test surface
    # killed per docs/decision-app-tests-2026-06-08.md.
    "python_package_import_failed",
    # v13 — scheduled-action contracts (docs/spec-audit-extensions-2026-05-17.md §3.3)
    "scheduled_action_evidence_path",
    "scheduled_action_anchor",
    "scheduled_action_input_missing",
    "heartbeat_anchors_present",
    "cron_labels_loaded",
    "scheduled_action_section_drift",
    # v16 — install-site verification (docs/spec-forge-side-effects-2026-06-02.md §7)
    # A1: scheduled_action.installed_artifact resolves to a live install.
    "scheduled_action_install_missing",
    # A2: scheduled_action.install.command's script path resolves under workspace
    # (full dry-run execution is out of scope for the structural pass).
    "scheduled_action_command_unresolvable",
    # A5: outputs[].kind=session_message cross-checks with A1 — if the action
    # declares a session-message output but A1 found no hook registration,
    # the output channel is dead.
    "scheduled_action_output_channel_invalid",
    # A6: per-bot orphan check — a LaunchAgent or hook attributable to this
    # bot that no manifest claims via scheduled_actions[].installed_artifact.
    "scheduled_action_orphan_install",
    # v20 — openclaw cron run status check
    # (docs/spec-app-coherence-and-reconciliation-2026-06-05.md §17.3 + Q32).
    # The load-bearing assertion: catches crons whose schedule fires but
    # whose work fails. Validation against the production pod surfaced
    # 14 of 16 scheduled jobs in error/skipped state — none of which
    # operators would notice without this check.
    #
    #   openclaw_cron_error             — Status: "error" on last run (critical)
    #   openclaw_cron_skipped           — Status: "skipped" >2 intervals (major)
    #   openclaw_cron_delivery_failure  — Status: "ok" but summary contains
    #                                     failure-language patterns indicating
    #                                     side-channel delivery broke (major)
    "openclaw_cron_error",
    "openclaw_cron_skipped",
    "openclaw_cron_delivery_failure",
    # v21 — discoverability (whether the bot's LLM can see + route to the app).
    # The fields below feed packages/admin/evolve_admin/applications/
    # app_registry.py:render_installed_apps_md, which is what the bot reads at
    # session start to decide whether to invoke an app. A manifest can pass
    # every structural check above and still be conversationally invisible
    # because these renderer fields are empty — the app sits installed but the
    # LLM has no way to know when/how to call it. Connects to the
    # agent-freelance-bypass class of failures: when the LLM doesn't know an
    # app exists, it freelances on general tools and bypasses scope/grounding.
    #
    # Routing-only assertions skip apps whose usage.model is "scheduled" or
    # "event-driven" — those don't need user-initiated routing surface.
    "app_discoverability_no_invocation_model",
    "app_discoverability_no_how_to_use",
    "app_discoverability_thin_hint_words",
    "app_discoverability_no_example_triggers",
    "app_discoverability_no_cli",
    # v22 — per-turn bootstrap-cost discipline
    # (docs/principle-apps-minimize-bootstrap-cost.md).
    # Calibration phase: all three fire at SEVERITY_INFO. Tune the
    # thresholds against real production data; promote the highest-
    # leverage check to SEVERITY_MINOR/MAJOR after one calibration
    # window.
    #
    # The bot-level aggregate (sum of bot_guidance + INSTALLED_APPS
    # entries across all installed apps) is NOT a per-manifest check —
    # it's computed in app_bootstrap_footprint.compute_app_bootstrap_footprint
    # and surfaced by the chip + a runner-side Signal emission, since
    # the assertion runner is per-manifest by design.
    #
    #   app_bot_guidance_oversized       — bot_guidance bytes > 1 KB
    #   app_invocation_mode_not_subagent — has CLI scripts but routes to bot main session
    #   app_cron_eligible_used_heartbeat — declares heartbeat_evidence with no LLM intent
    "app_bot_guidance_oversized",
    "app_invocation_mode_not_subagent",
    "app_cron_eligible_used_heartbeat",
    # v23 — producer surface presence
    # (companion to PR #2476's platform-files defense). Fires at major
    # when a *designed* manifest declares no input/trigger surface at all
    # — no schedule, no heartbeat anchor, no cron, no event trigger, no
    # CLI. This is the structural-audit catch for the "empty shell"
    # anti-pattern: an app whose name exists but whose machinery is
    # undocumented. Catches the Session-Turn-Logs class before L3's
    # platform-files sweep gets to it on the next scan, and catches new
    # variants where the file footprint isn't platform-written (so L3
    # leaves it alone) but no producer is declared either.
    # Passive-app calibration (2026-06-13, companion to R1 verifier-noise
    # PR #2828): an OBSERVATIONAL scanner-minted Instance with attributed
    # data files (a logger / tracker / workspace) is a working passive
    # store, not a defect — it does NOT fire (the absence of producer
    # machinery is inherent to observed-not-designed apps). The signal
    # only fires when an app is SUPPOSED to deliver (declares a
    # delivery_contract / outputs[]) but has no surface to do so. See
    # check_app_has_producer_surface.
    "app_no_producer_surface",
    # v24 — v7-arc Instance → Spec binding integrity.
    # Fires when a v7-arc Instance can't hydrate from its bound Spec:
    # either provenance.spec_id is missing/null, or the Spec file
    # doesn't exist at the canonical
    # {shared_dir}/gallery/{local,builtin,imported/<pod>}/<spec_id>/<spec_version>.json
    # path. Surveyed pod-wide 2026-06-09: 5 of 23 v7-arc Instances had
    # spec_id: None (team-bot-a, all observationally-minted by the scanner).
    # The downstream symptom was hydration silently returning the bare
    # Instance, every subsequent audit firing app_no_producer_surface,
    # and operators chasing 100+ alerts driven by one root cause.
    # Surfacing the binding gap directly gives operators the actionable
    # fix (re-Spec the Instance or archive it) instead of seeing only
    # the audit's cascade.
    "orphan_v7_arc_instance",
    # v25 — manifest-v23 delivery_contract{} on scheduled_actions[]
    # (spec-proactive-delivery-monitor-2026-06-10.md §5 + §11). Tier-2
    # owns the static contract: a malformed block (the delivery_monitor
    # falls back to derived defaults, so the author's declared windows /
    # heal assertion silently don't apply until fixed), and a declared
    # run_file evidence path that doesn't appear in
    # interface_contract.data_files (the §5.4 declaration requirement —
    # evidence the app never claims to write can't prove deliveries).
    # Window-level timeliness stays with the delivery_monitor daemon;
    # Tier-2 must not grow per-window checks (§11 boundary).
    "delivery_contract_invalid",
    "delivery_contract_evidence_undeclared",
)


# Layers whose contents are expected to change between audits. sha drift on
# these is benign — the data IS the point of the file, and a stable sha
# would actually mean the app stopped working.
_VOLATILE_LAYERS = frozenset({"data", "state"})


# ── Layer-aware severity (v20 spec §3.2) ───────────────────────────────────
#
# PR 5: severity for missing-file / sha-drift findings now depends on the
# file's layer. A missing ``code`` file is critical (app is broken); a
# missing ``data`` file is info (probably just rotation / cleanup).
# Without this gate, every data-layer file rotation fires alert-tier
# Signals → operator chat fatigue → mute the producer → real findings
# get missed.
#
# The tables encode the matrix exactly as spec §3.2 describes. ``None``
# means "produce no finding at all" (the spec's "ignored" entries —
# e.g., sha drift on data/log/state files isn't worth flagging).

_MISSING_FILE_SEVERITY: dict[str, str] = {
    "code":         SEVERITY_CRITICAL,
    "config":       SEVERITY_CRITICAL,
    "contract":     SEVERITY_CRITICAL,
    "behavior_doc": SEVERITY_CRITICAL,
    "reference":    SEVERITY_MINOR,    # "warning" in spec; minor is closest
    "content":      SEVERITY_MAJOR,
    "data":         SEVERITY_INFO,
    "log":          SEVERITY_INFO,
    "state":        SEVERITY_INFO,
    # Legacy v19 layer value before PR 1's "script"→"code" migration.
    # Treat as code so post-migration files (which still see "script"
    # in transient _legacy_layer paths) get the right severity.
    "script":       SEVERITY_CRITICAL,
    # Unknown / unset layer falls to critical (safe choice — better to
    # over-alert than silently swallow a real failure).
    "":             SEVERITY_CRITICAL,
}

_SHA_DRIFT_SEVERITY: dict[str, str | None] = {
    "code":         SEVERITY_MAJOR,
    "config":       SEVERITY_CRITICAL,
    "contract":     SEVERITY_CRITICAL,
    "behavior_doc": SEVERITY_MAJOR,
    "reference":    None,    # ignored
    "content":      None,    # ignored — content evolves by design
    "data":         None,
    "log":          None,
    "state":        None,
    "script":       SEVERITY_MAJOR,
    "":             SEVERITY_MAJOR,
}


def severity_for_missing_file(layer: str | None) -> str:
    """Return the v20 severity for a missing-file finding by layer.

    Falls to SEVERITY_CRITICAL on unknown layers — the safe choice.
    Spec §3.2.
    """
    return _MISSING_FILE_SEVERITY.get(
        (layer or "").lower(), SEVERITY_CRITICAL,
    )


def severity_for_sha_drift(layer: str | None) -> str | None:
    """Return the v20 severity for a sha-drift finding, or ``None`` to
    skip the finding entirely (spec §3.2 "ignored" entries).

    Caller pattern:

        sev = severity_for_sha_drift(rec.get("layer"))
        if sev is None:
            continue
        findings.append(Finding(severity=sev, ...))
    """
    return _SHA_DRIFT_SEVERITY.get((layer or "").lower(), SEVERITY_MAJOR)


# ── Assertion functions ─────────────────────────────────────────────────────
#
# Each function takes the manifest dict + a context dict (bot_user, workspace
# path, crontab snapshot, etc.) and returns a list of Finding. Pure functions
# — they read state from the inputs only and have no side effects.


def check_files_exist(manifest: dict, ctx: dict) -> list[Finding]:
    """Every entry in manifest.files[*].path exists on disk.

    **PR 5: Severity is now layer-aware** per spec §3.2. A missing
    ``code`` / ``config`` / ``contract`` / ``behavior_doc`` file is
    critical (app structurally broken); a missing ``content`` file is
    major; a missing ``reference`` is minor; missing ``data`` /
    ``log`` / ``state`` files are info (rotation / cleanup is expected).
    """
    findings: list[Finding] = []
    workspace: Path = ctx["workspace"]
    for rec in manifest.get("files") or []:
        if not isinstance(rec, dict):
            continue
        path = (rec.get("path") or "").lstrip("/")
        if not path:
            continue
        if not (workspace / path).exists():
            layer = rec.get("layer", "")
            findings.append(Finding(
                assertion_id="file_missing",
                severity=severity_for_missing_file(layer),
                summary=f"manifest claims file {path!r} but it is missing on disk",
                evidence={"path": path, "layer": layer},
            ))
    return findings


def check_files_sha(manifest: dict, ctx: dict) -> list[Finding]:
    """sha256 of each non-volatile file matches the manifest's recorded value.

    Skips files whose ``layer`` is in ``_VOLATILE_LAYERS`` (data/state files
    are expected to change). Skips files with no recorded sha.
    Missing files are not flagged here — ``check_files_exist`` owns that.
    """
    findings: list[Finding] = []
    workspace: Path = ctx["workspace"]
    for rec in manifest.get("files") or []:
        if not isinstance(rec, dict):
            continue
        path = (rec.get("path") or "").lstrip("/")
        if not path:
            continue
        if rec.get("layer", "") in _VOLATILE_LAYERS:
            continue
        expected = (rec.get("sha256") or "").strip().lower()
        if not expected:
            continue
        full = workspace / path
        if not full.exists():
            continue   # owned by check_files_exist
        try:
            actual = hashlib.sha256(full.read_bytes()).hexdigest()
        except Exception as exc:
            # Unreadable file is a separate failure mode — surface as major.
            findings.append(Finding(
                assertion_id="file_sha_mismatch",
                severity=SEVERITY_MAJOR,
                summary=f"could not read {path!r} to verify sha256: {exc}",
                evidence={"path": path, "error": str(exc)},
            ))
            continue
        if actual != expected:
            # PR 5: severity by layer per spec §3.2. None = ignored
            # (the spec's "ignored" entries — sha drift on data / log /
            # state / content / reference files isn't worth flagging
            # because those layers either evolve by design or carry no
            # contract about content).
            layer = rec.get("layer", "")
            sev = severity_for_sha_drift(layer)
            if sev is None:
                continue
            findings.append(Finding(
                assertion_id="file_sha_mismatch",
                severity=sev,
                summary=f"sha256 drift on {path!r}",
                evidence={
                    "path": path,
                    "expected_sha": expected,
                    "actual_sha": actual,
                    "layer": layer,
                },
            ))
    return findings


def check_cron_scripts_exist(manifest: dict, ctx: dict) -> list[Finding]:
    """Every entry in manifest.crons[*].script exists.

    The script may be path-relative-to-workspace or absolute; both are tried.
    Cron entries without a parseable script are skipped (covered by
    ``check_cron_schedules`` instead).
    """
    findings: list[Finding] = []
    workspace: Path = ctx["workspace"]
    for entry in manifest.get("crons") or []:
        script = _cron_script(entry)
        if not script:
            continue
        if _resolve_script_path(script, workspace) is None:
            findings.append(Finding(
                assertion_id="cron_script_missing",
                severity=SEVERITY_CRITICAL,
                summary=f"cron references script {script!r} that does not exist",
                evidence={"script": script, "cron_schedule": _cron_schedule(entry)},
            ))
    return findings


_CRON_FIELD_RE = re.compile(r"^[\d\*\-\,\/]+$")
_CRON_KEYWORDS = {
    "@reboot", "@yearly", "@annually", "@monthly", "@weekly",
    "@daily", "@midnight", "@hourly",
}


def check_cron_schedules(manifest: dict, ctx: dict) -> list[Finding]:
    """Every cron schedule string parses as a valid crontab schedule.

    Accepts standard 5-field schedules and @keyword forms. Empty / malformed
    entries get a ``major`` finding because the cron won't fire at all.
    """
    findings: list[Finding] = []
    for entry in manifest.get("crons") or []:
        schedule = _cron_schedule(entry)
        if not schedule:
            findings.append(Finding(
                assertion_id="cron_schedule_unparseable",
                severity=SEVERITY_MAJOR,
                summary="cron entry has no schedule",
                evidence={"entry": entry},
            ))
            continue
        if not _parse_cron_schedule(schedule):
            findings.append(Finding(
                assertion_id="cron_schedule_unparseable",
                severity=SEVERITY_MAJOR,
                summary=f"cron schedule {schedule!r} does not parse",
                evidence={"schedule": schedule, "script": _cron_script(entry)},
            ))
    return findings


def check_crons_installed(manifest: dict, ctx: dict) -> list[Finding]:
    """Every manifest cron appears in the live crontab.

    Reads ``ctx["crontab_lines"]`` (the runner snapshots ``crontab -l`` once
    per audit run so we don't fork per assertion). Compares schedule + script
    pair, normalized to whitespace-collapsed strings.
    """
    findings: list[Finding] = []
    live = ctx.get("crontab_lines") or []
    if not live and not (manifest.get("crons") or []):
        return findings
    live_normalized = {_normalize_cron_line(line) for line in live}
    for entry in manifest.get("crons") or []:
        schedule = _cron_schedule(entry)
        script = _cron_script(entry)
        if not schedule or not script:
            continue   # handled by other assertions
        expected = _normalize_cron_line(f"{schedule} {script}")
        # Tolerate the script appearing in any position after schedule, since
        # actual crontab lines may have leading "cd ... &&" or similar prefixes.
        if expected in live_normalized:
            continue
        # Soft match: schedule + script-token present in any live line
        if any(_cron_soft_match(schedule, script, line) for line in live_normalized):
            continue
        findings.append(Finding(
            assertion_id="cron_not_in_crontab",
            severity=SEVERITY_MAJOR,
            summary=f"cron {schedule!r} for {script!r} is missing from crontab -l",
            evidence={"schedule": schedule, "script": script},
        ))
    return findings


# check_test_command removed 2026-06-08 — app-test surface killed per
# docs/decision-app-tests-2026-06-08.md.


def check_python_packages(manifest: dict, ctx: dict) -> list[Finding]:
    """Every required python_packages import succeeds in the bot's env.

    Each entry's ``import`` field is what gets attempted, falling back to the
    pip_name when import isn't set. Optional packages (required=False) are
    skipped to avoid noise on apps that gracefully degrade.
    """
    findings: list[Finding] = []
    requirements = manifest.get("requirements") or {}
    pkgs = requirements.get("python_packages") or []
    python_bin = ctx.get("python_bin") or "python3"
    for rec in pkgs:
        if not isinstance(rec, dict):
            continue
        if not rec.get("required", True):
            continue
        import_name = rec.get("import") or rec.get("pip_name") or ""
        if not import_name:
            continue
        try:
            result = subprocess.run(
                [python_bin, "-c", f"import {import_name}"],
                capture_output=True, timeout=10,
            )
        except Exception as exc:
            findings.append(Finding(
                assertion_id="python_package_import_failed",
                severity=SEVERITY_MAJOR,
                summary=f"could not probe import for package {import_name!r}: {exc}",
                evidence={"package": import_name, "error": str(exc)},
            ))
            continue
        if result.returncode != 0:
            findings.append(Finding(
                assertion_id="python_package_import_failed",
                severity=SEVERITY_MAJOR,
                summary=f"required python package {import_name!r} does not import",
                evidence={
                    "package": import_name,
                    "pip_name": rec.get("pip_name", ""),
                    "stderr": result.stderr.decode("utf-8", "replace")[:400],
                },
            ))
    return findings


# ── v13: scheduled-action contract assertions ───────────────────────────────
#
# These verify the scheduled_actions[] / heartbeat_evidence / cron_evidence
# fields added by the scanner's extraction pass. The "anchor" assertion is
# the protein-reminder catch: a heartbeat surface section that names the
# scheduled action must still resolve when audit runs. If the heartbeat
# gets clobbered or the section is removed, this fires `critical`.
#
# See docs/spec-audit-extensions-2026-05-17.md §3.3.


# When a section's content sha changes by more than this fraction of bytes
# since the manifest was last written, emit a `minor` drift finding. The
# threshold is intentionally lenient — small edits shouldn't nag operators.
_SECTION_DRIFT_THRESHOLD = 0.50


def check_scheduled_action_evidence_paths(manifest: dict, ctx: dict) -> list[Finding]:
    """Every scheduled_actions[*].trigger.evidence_path exists on disk.

    Missing evidence is `critical` — the contract claim points at a file
    that's not there, so the trigger can't be verified at all.
    """
    findings: list[Finding] = []
    workspace: Path = ctx["workspace"]
    for action in manifest.get("scheduled_actions") or []:
        if not isinstance(action, dict):
            continue
        trigger = action.get("trigger") or {}
        path = (trigger.get("evidence_path") or "").lstrip("/")
        action_id = action.get("id") or "?"
        if not path:
            continue
        if not (workspace / path).exists():
            findings.append(Finding(
                assertion_id="scheduled_action_evidence_path",
                severity=SEVERITY_CRITICAL,
                summary=(
                    f"scheduled action {action_id!r} cites evidence file "
                    f"{path!r} but it is missing on disk"
                ),
                evidence={
                    "path": path,
                    "action_id": action_id,
                    "trigger_kind": trigger.get("kind", ""),
                },
            ))
    return findings


def check_scheduled_action_anchors(manifest: dict, ctx: dict) -> list[Finding]:
    """Every scheduled_actions[*].trigger.evidence_locator resolves in its file.

    Locator is a heading text or unique phrase. If it's not findable in the
    evidence file, the cited section has been removed / clobbered — emit
    `critical` (this is the protein-reminder catch).
    """
    findings: list[Finding] = []
    workspace: Path = ctx["workspace"]
    for action in manifest.get("scheduled_actions") or []:
        if not isinstance(action, dict):
            continue
        trigger = action.get("trigger") or {}
        path = (trigger.get("evidence_path") or "").lstrip("/")
        locator = (trigger.get("evidence_locator") or "").strip()
        action_id = action.get("id") or "?"
        if not path or not locator:
            continue
        full = workspace / path
        if not full.exists():
            continue   # owned by check_scheduled_action_evidence_paths
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            findings.append(Finding(
                assertion_id="scheduled_action_anchor",
                severity=SEVERITY_MAJOR,
                summary=(
                    f"could not read {path!r} to resolve anchor for "
                    f"scheduled action {action_id!r}: {exc}"
                ),
                evidence={"path": path, "action_id": action_id, "error": str(exc)},
            ))
            continue
        if not _anchor_present(text, locator):
            findings.append(Finding(
                assertion_id="scheduled_action_anchor",
                severity=SEVERITY_CRITICAL,
                summary=(
                    f"scheduled action {action_id!r} anchor not found in "
                    f"{path!r} — heartbeat surface may have been clobbered"
                ),
                evidence={
                    "path": path,
                    "action_id": action_id,
                    "locator": locator[:200],
                },
            ))
    return findings


def check_scheduled_action_inputs(manifest: dict, ctx: dict) -> list[Finding]:
    """Every scheduled_actions[*].inputs[*].path exists (unless kind=external).

    Inputs that the action reads from must exist for the action to do its
    job. `external` inputs (network APIs, etc.) are skipped — there's
    nothing for a structural check to verify.
    """
    findings: list[Finding] = []
    workspace: Path = ctx["workspace"]
    for action in manifest.get("scheduled_actions") or []:
        if not isinstance(action, dict):
            continue
        action_id = action.get("id") or "?"
        for inp in action.get("inputs") or []:
            if not isinstance(inp, dict):
                continue
            if (inp.get("kind") or "").lower() == "external":
                continue
            path = (inp.get("path") or "").lstrip("/")
            if not path:
                continue
            if not (workspace / path).exists():
                findings.append(Finding(
                    assertion_id="scheduled_action_input_missing",
                    severity=SEVERITY_MAJOR,
                    summary=(
                        f"scheduled action {action_id!r} input file {path!r} "
                        f"is missing"
                    ),
                    evidence={
                        "path": path,
                        "action_id": action_id,
                        "input_kind": inp.get("kind", "data_file"),
                    },
                ))
    return findings


def check_heartbeat_anchors(manifest: dict, ctx: dict) -> list[Finding]:
    """Every heartbeat_evidence.section_anchors[*] is present in cited file.

    `critical` because heartbeat surfaces are the standing-instruction
    layer: if the section is gone, the bot's standing routines have
    silently changed. This is the spec's load-bearing assertion for
    the protein-reminder failure class.
    """
    findings: list[Finding] = []
    workspace: Path = ctx["workspace"]
    he = manifest.get("heartbeat_evidence") or {}
    if not isinstance(he, dict):
        return findings
    file_path = (he.get("file_path") or "").lstrip("/")
    anchors = he.get("section_anchors") or []
    if not file_path or not anchors:
        return findings
    full = workspace / file_path
    if not full.exists():
        # Missing heartbeat file entirely
        findings.append(Finding(
            assertion_id="heartbeat_anchors_present",
            severity=SEVERITY_CRITICAL,
            summary=(
                f"heartbeat evidence file {file_path!r} is missing"
            ),
            evidence={"path": file_path, "anchors": list(anchors)[:5]},
        ))
        return findings
    try:
        text = full.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        findings.append(Finding(
            assertion_id="heartbeat_anchors_present",
            severity=SEVERITY_MAJOR,
            summary=f"could not read heartbeat file {file_path!r}: {exc}",
            evidence={"path": file_path, "error": str(exc)},
        ))
        return findings
    for anchor in anchors:
        if not isinstance(anchor, str) or not anchor.strip():
            continue
        if not _anchor_present(text, anchor):
            findings.append(Finding(
                assertion_id="heartbeat_anchors_present",
                severity=SEVERITY_CRITICAL,
                summary=(
                    f"heartbeat anchor {anchor[:80]!r} missing from "
                    f"{file_path!r}"
                ),
                evidence={"path": file_path, "anchor": anchor[:200]},
            ))
    return findings


def check_cron_labels_loaded(manifest: dict, ctx: dict) -> list[Finding]:
    """Every cron_evidence.labels[*] appears in launchctl or crontab.

    Labels are launchd plist labels (e.g. ``com.user.morning-briefing``)
    or crontab entry tags. The runner snapshots both before calling this,
    so it's a simple membership check.
    """
    findings: list[Finding] = []
    ce = manifest.get("cron_evidence") or {}
    if not isinstance(ce, dict):
        return findings
    labels = ce.get("labels") or []
    if not labels:
        return findings
    launchctl_labels = set(ctx.get("launchctl_labels") or [])
    crontab_text = "\n".join(ctx.get("crontab_lines") or [])
    for label in labels:
        if not isinstance(label, str) or not label.strip():
            continue
        loaded = (
            label in launchctl_labels
            or any(label in line for line in (ctx.get("crontab_lines") or []))
            or label in crontab_text
        )
        if not loaded:
            findings.append(Finding(
                assertion_id="cron_labels_loaded",
                severity=SEVERITY_MAJOR,
                summary=(
                    f"cron label {label!r} not loaded in launchctl or crontab"
                ),
                evidence={"label": label},
            ))
    return findings


# ── v16 install-site verification (spec-forge-side-effects §7) ───────────────
#
# Four new assertions layered on top of the existing scheduled-action checks.
# A3 (inputs exist) is already covered by check_scheduled_action_inputs above;
# A4 (evidence anchor) by check_scheduled_action_anchors. The four below add:
#
#   A1 — install present       → check_scheduled_action_install_present
#   A2 — command resolvable    → check_scheduled_action_command_resolvable
#   A5 — output channel valid  → check_scheduled_action_output_channel
#   A6 — orphan installs       → check_orphan_install_artifacts
#
# ctx additions consumed (all optional; assertion no-ops cleanly if absent):
#   - openclaw_hooks_block: dict — parsed `hooks` block from the bot's
#       openclaw.json (or {} when scanner didn't read it). Used by A1 to
#       resolve `openclaw.json#hooks.<event>` artifacts.
#   - bot_launchd_entries: list[dict] — full enumerated plists from
#       scanner._enumerate_launch_agents. Used by A1 to verify launchd
#       installs and by A6 to enumerate orphans.
#   - all_pod_installed_artifacts: set[str] — union of every
#       scheduled_actions[].installed_artifact across every manifest on the
#       bot. Used by A6 to decide what's orphan.


_INSTALLED_ARTIFACT_LAUNCHD_RE = re.compile(
    r".*/Library/LaunchAgents/(?P<label>[^/]+)\.plist$"
)
_INSTALLED_ARTIFACT_OC_HOOK_RE = re.compile(
    r"^openclaw\.json#hooks\.(?P<event>[A-Za-z_][A-Za-z0-9_]*)$"
)
# v17: ``HEARTBEAT.md#Section Anchor`` (anchor is the heading text after
# stripping leading ``#``s + whitespace). Used by A1 to look up the
# managed section in the bot's workspace.
_INSTALLED_ARTIFACT_INSTRUCTION_RE = re.compile(
    r"^(?P<file>[A-Za-z][A-Za-z0-9_./-]*\.md)#(?P<anchor>.+)$"
)
# Marker that identifies a section as evolve-managed. Must match the
# pattern install_heartbeat_instruction emits. The ``pkg`` field carries
# the owning app's package id for attribution.
_MANAGED_MARKER_RE = re.compile(
    r"<!--\s*evolve-managed(?::\s*[^>]*)?\s*-->",
    re.IGNORECASE,
)


def _hook_command_in_openclaw(
    hooks_block: dict, event: str, app_evidence_files: list[str],
) -> bool:
    """DEPRECATED in v17 — see _instruction_section_in_workspace.

    OpenClaw has no ``hooks.heartbeat[]`` array in its config schema
    (the design-bug PR 9 documents). Kept for one schema version so the
    deprecated mechanism handling can still emit a useful evidence dict.
    Always returns False.
    """
    del hooks_block, event, app_evidence_files
    return False


def _instruction_section_present(
    workspace: Path, file: str, anchor: str, command: str,
) -> tuple[bool, str]:
    """Does workspace/{file} contain a section named ``anchor`` (heading
    text, no leading ``#``s) carrying the evolve-managed marker AND
    referencing ``command``?

    Returns (present, diagnostic). The diagnostic is non-empty when the
    section is missing the marker or the command — helps the operator
    triage whether the section was hand-edited away from forge's
    install vs. just renamed.
    """
    if not workspace or not file or not anchor:
        return False, "empty file/anchor"
    target = workspace / file
    if not target.exists():
        return False, f"file {file!r} does not exist in workspace"
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return False, f"could not read {file!r}: {exc}"

    # ``_extract_section`` normalizes heading text after stripping the
    # leading ``#``s, so we pass just the anchor (no ``#`` prefix).
    bare_anchor = anchor.lstrip("#").strip()
    section_text = _extract_section(text, bare_anchor)
    if section_text is None:
        return False, f"section anchor {anchor!r} not found in {file!r}"

    if not _MANAGED_MARKER_RE.search(section_text):
        return False, (
            f"section {anchor!r} in {file!r} is missing the "
            f"<!-- evolve-managed --> marker (operator hand-edited?)"
        )

    # Cross-check command appears in body. Tolerant of quoting / fragments.
    if command and command not in section_text:
        # Some commands include a path the body might shorten. Try the
        # leaf script name as a fallback.
        leaf = Path(command.split()[-1]).name if command.split() else ""
        if not leaf or leaf not in section_text:
            return False, (
                f"section {anchor!r} no longer references "
                f"install.command (drift between manifest and body)"
            )
    return True, ""


def check_scheduled_action_install_present(manifest: dict, ctx: dict) -> list[Finding]:
    """A1 — every scheduled_action with mechanism != unknown has a live install.

    Resolution per mechanism:
      - oc_heartbeat_instruction / oc_session_instruction (v17 default) →
        workspace/{file} has the section_anchor + evolve-managed marker;
        section body still references install.command
      - launchd → the plist file exists on disk AND its label appears in
        launchctl_labels (loaded), OR a backfill entry exists with the same
        label (loaded later by the user)
      - crontab → command/label appears in the crontab snapshot
      - oc_heartbeat_hook / oc_session_hook (deprecated v17) → flagged as
        major: manifest hasn't been migrated; suggest running migrate_manifest()
      - external / unknown → skipped (nothing for the structural check to
        verify)

    Missing installs are `major` — the manifest's contract isn't honored,
    but the app may still partially work (the missing automation is the
    failure mode, not the whole app).
    """
    findings: list[Finding] = []
    workspace: Path = ctx["workspace"]
    launchd_entries = ctx.get("bot_launchd_entries") or []
    launchctl_labels = set(ctx.get("launchctl_labels") or [])
    crontab_lines = ctx.get("crontab_lines") or []

    # Index launchd plists by label for O(1) lookup
    launchd_by_label: dict[str, dict] = {}
    for entry in launchd_entries:
        if isinstance(entry, dict):
            label = entry.get("label") or ""
            if label:
                launchd_by_label[label] = entry

    for action in manifest.get("scheduled_actions") or []:
        if not isinstance(action, dict):
            continue
        mechanism = (action.get("mechanism") or "").strip()
        action_id = action.get("id") or "?"
        if mechanism in ("", "unknown", "external"):
            continue
        artifact = (action.get("installed_artifact") or "").strip()

        present = False
        evidence: dict = {"action_id": action_id, "mechanism": mechanism}

        if mechanism in ("oc_heartbeat_instruction", "oc_session_instruction"):
            # v17: parse artifact as ``{file}#{anchor}`` and verify the
            # workspace file has the managed section.
            install = action.get("install") or {}
            file = (install.get("file") or "").strip()
            anchor = (install.get("section_anchor") or "").lstrip("# ").strip()
            command = (install.get("command") or "").strip()
            # Fall back to parsing the artifact if install block didn't
            # carry both fields (e.g. migrated v16 manifest without the
            # full v17 install recipe yet).
            if not file or not anchor:
                m = _INSTALLED_ARTIFACT_INSTRUCTION_RE.match(artifact)
                if m:
                    file = file or m.group("file")
                    anchor = anchor or m.group("anchor")
            evidence["file"] = file
            evidence["anchor"] = anchor
            evidence["artifact"] = artifact
            if file and anchor:
                present, diag = _instruction_section_present(
                    workspace, file, anchor, command,
                )
                if diag:
                    evidence["diagnostic"] = diag
        elif mechanism in ("oc_heartbeat_hook", "oc_session_hook"):
            # Deprecated in v17. Surface as install-missing with explicit
            # remediation pointer — running migrate_manifest() rewrites
            # the mechanism and clears the bogus installed_artifact, and
            # the next forge run materializes via the correct surface.
            findings.append(Finding(
                assertion_id="scheduled_action_install_missing",
                severity=SEVERITY_MAJOR,
                summary=(
                    f"scheduled action {action_id!r} carries deprecated "
                    f"mechanism={mechanism}; run migrate_manifest() then "
                    f"re-forge (spec-heartbeat-instruction-2026-06-03 §9)"
                ),
                evidence={
                    "action_id": action_id, "mechanism": mechanism,
                    "artifact": artifact,
                },
            ))
            continue
        elif mechanism == "launchd":
            install = action.get("install") or {}
            label = (install.get("plist_label") or "").strip()
            # Derive label from artifact if install block didn't carry it
            if not label:
                m = _INSTALLED_ARTIFACT_LAUNCHD_RE.match(artifact)
                if m:
                    label = m.group("label")
            evidence["label"] = label
            evidence["artifact"] = artifact
            if label:
                # Either we enumerated the plist on disk, or it's loaded in
                # launchctl. Either is sufficient evidence the install exists.
                present = (label in launchd_by_label) or (label in launchctl_labels)
        elif mechanism == "crontab":
            install = action.get("install") or {}
            label = (install.get("label") or "").strip()
            command = (install.get("command") or "").strip()
            evidence["label"] = label
            for line in crontab_lines:
                if (label and label in line) or (command and command in line):
                    present = True
                    break
        else:
            # Unknown mechanism enum value — surface as invalid rather than
            # silently passing.
            findings.append(Finding(
                assertion_id="scheduled_action_install_missing",
                severity=SEVERITY_MINOR,
                summary=(
                    f"scheduled action {action_id!r} has unrecognized "
                    f"mechanism {mechanism!r}"
                ),
                evidence=evidence,
            ))
            continue

        if not present:
            findings.append(Finding(
                assertion_id="scheduled_action_install_missing",
                severity=SEVERITY_MAJOR,
                summary=(
                    f"scheduled action {action_id!r} declares "
                    f"mechanism={mechanism} but no matching install was "
                    f"found on disk"
                ),
                evidence=evidence,
            ))
    return findings


def check_scheduled_action_command_resolvable(
    manifest: dict, ctx: dict,
) -> list[Finding]:
    """A2 — every scheduled_action.install.command's script resolves on disk.

    Lighter-weight than a true dry-run (which raises security + runtime
    concerns out of scope for the structural pass). We just confirm the
    *script* the command invokes resolves under the workspace — typically
    a ``python3 scripts/tasks.py check`` is a pass when ``scripts/tasks.py``
    exists. Empty / missing install.command is silently skipped (A1 owns
    the "should there be one" question).
    """
    findings: list[Finding] = []
    workspace: Path = ctx["workspace"]
    for action in manifest.get("scheduled_actions") or []:
        if not isinstance(action, dict):
            continue
        install = action.get("install") or {}
        command = (install.get("command") or "").strip()
        action_id = action.get("id") or "?"
        if not command:
            continue
        # Reuse the cron resolver — same shape (interpreter + script + args)
        resolved = _resolve_script_path(command, workspace)
        if resolved is None:
            findings.append(Finding(
                assertion_id="scheduled_action_command_unresolvable",
                severity=SEVERITY_MAJOR,
                summary=(
                    f"scheduled action {action_id!r} install.command "
                    f"references a script that does not resolve under "
                    f"workspace"
                ),
                evidence={
                    "action_id": action_id,
                    "command": command[:200],
                },
            ))
    return findings


def check_scheduled_action_output_channel(
    manifest: dict, ctx: dict,
) -> list[Finding]:
    """A5 — outputs[].kind=session_message requires a live install surface.

    A scheduled action that promises to deliver to the bot's session needs
    a HEARTBEAT.md / AGENTS.md instruction (or session hook) the bot LLM
    will actually read on its next turn — otherwise the output goes nowhere
    the bot can see. This assertion only fires for actions that BOTH declare
    a session_message output AND have an instruction-based mechanism,
    AND A1's resolver finds no matching managed section.
    """
    findings: list[Finding] = []
    workspace: Path = ctx["workspace"]
    for action in manifest.get("scheduled_actions") or []:
        if not isinstance(action, dict):
            continue
        outputs = action.get("outputs") or []
        if not isinstance(outputs, list):
            continue
        has_session_output = any(
            isinstance(o, dict) and (o.get("kind") or "").lower() == "session_message"
            for o in outputs
        )
        if not has_session_output:
            continue
        mechanism = (action.get("mechanism") or "").strip()
        action_id = action.get("id") or "?"
        # Only meaningful for instruction-based mechanisms in v17 (and the
        # deprecated hook variants, which always fail). A launchd-only
        # action with a session-message output is its own design problem
        # caught elsewhere.
        if mechanism not in (
            "oc_heartbeat_instruction", "oc_session_instruction",
            "oc_heartbeat_hook", "oc_session_hook",
        ):
            continue
        # Deprecated hook mechanisms can never have a valid section — A1
        # already reports them. Skip to avoid double-reporting.
        if mechanism in ("oc_heartbeat_hook", "oc_session_hook"):
            continue
        install = action.get("install") or {}
        file = (install.get("file") or "").strip()
        anchor = (install.get("section_anchor") or "").lstrip("# ").strip()
        command = (install.get("command") or "").strip()
        if not file or not anchor:
            artifact = (action.get("installed_artifact") or "").strip()
            m = _INSTALLED_ARTIFACT_INSTRUCTION_RE.match(artifact)
            if m:
                file = file or m.group("file")
                anchor = anchor or m.group("anchor")
        present = False
        if file and anchor:
            present, _diag = _instruction_section_present(
                workspace, file, anchor, command,
            )
        if not present:
            findings.append(Finding(
                assertion_id="scheduled_action_output_channel_invalid",
                severity=SEVERITY_MAJOR,
                summary=(
                    f"scheduled action {action_id!r} declares "
                    f"outputs[].kind=session_message but no managed "
                    f"section is registered in workspace/{file} "
                    f"to deliver it"
                ),
                evidence={
                    "action_id": action_id,
                    "mechanism": mechanism,
                    "file": file,
                    "anchor": anchor,
                },
            ))
    return findings


def _is_evolve_managed_label(label: str, bot_id: str) -> bool:
    """True iff ``label`` is in this bot's Evolve-managed launchd namespace.

    Forge installs scheduled-action LaunchAgents/Daemons under the canonical
    ``ai.evolve.{bot_id}.{app_slug}`` namespace (the post-2026-06-04 rename,
    enforced by the gallery regression test and the evolve user's sudoers
    grant scope) and the legacy ``com.{bot_id}.{app_slug}`` namespace
    (pre-rename installs + some hand-installs). A third-party LaunchAgent that
    merely happens to live in the bot user's ``~/Library/LaunchAgents``
    (Dropbox, Apple, Google updaters, …) is NOT an Evolve app install and must
    not be treated as an orphan. Scoping A6 to these namespaces is what keeps
    it from flagging ``com.dropbox.dropboxmacupdate.xpcservice`` and friends.

    The trailing ``.`` after the bot id keeps a bot whose id is a prefix of
    another (``bot-a`` vs ``bot-a-2``) from claiming the other's plists.
    """
    if not label or not bot_id:
        return False
    low = label.lower()
    bid = bot_id.lower()
    return low.startswith(f"ai.evolve.{bid}.") or low.startswith(f"com.{bid}.")


def check_orphan_install_artifacts(manifest: dict, ctx: dict) -> list[Finding]:
    """A6 — install sites on the bot that no manifest claims.

    Reverse direction of A1. Walks the bot's LaunchAgents and the
    evolve-managed sections in workspace HEARTBEAT.md / AGENTS.md; for
    each install plausibly attributable to one of the bot's apps, confirms
    SOME manifest claims it via ``installed_artifact``. Orphans surface as
    `minor` findings — typically either (a) an app was uninstalled but its
    install site wasn't cleaned up, or (b) a forge install attributed
    incorrectly and the right app is missing its entry.

    ``ctx["all_pod_installed_artifacts"]`` is the union of artifacts across
    all manifests on the bot — when absent (e.g. a single-manifest test),
    this assertion no-ops cleanly. Cross-manifest visibility is the runner's
    responsibility (it iterates manifests, builds the union, then calls each
    assertion with the union in ctx).
    """
    findings: list[Finding] = []
    claimed = ctx.get("all_pod_installed_artifacts")
    if claimed is None:
        # Runner hasn't computed the union — skip rather than emit false
        # orphans against a single-manifest view.
        return findings
    if not isinstance(claimed, (set, frozenset)):
        claimed = set(claimed)

    bot_id = (manifest.get("bot_id") or "").lower()

    # 1) LaunchAgents matching the bot's namespace must appear in claimed.
    for entry in ctx.get("bot_launchd_entries") or []:
        if not isinstance(entry, dict):
            continue
        label = entry.get("label") or ""
        plist_path = entry.get("plist_path") or ""
        # Scope orphan detection to Evolve-managed labels only. A LaunchAgent is
        # an orphan *app install* only when its label is in this bot's Evolve
        # namespace (ai.evolve.{bot}.* / com.{bot}.*). The prior heuristic also
        # attributed any plist whose path lived under /Users/{bot}/, which meant
        # every third-party LaunchAgent the bot user happened to have installed
        # (Dropbox, Apple, Google updaters) in ~/Library/LaunchAgents was
        # flagged as an unclaimed app daemon — that false-positive class was 62%
        # of one test pod's firing structural signals (2026-06-12 review).
        if not _is_evolve_managed_label(label, bot_id):
            continue
        # Only emit an orphan once per bot — the FIRST manifest that runs
        # this check claims responsibility for reporting. Without this,
        # every app's manifest would emit the same orphan, multiplying
        # noise N-fold (one finding per app per orphan).
        if plist_path and plist_path not in claimed and label not in claimed:
            findings.append(Finding(
                assertion_id="scheduled_action_orphan_install",
                severity=SEVERITY_MINOR,
                summary=(
                    f"LaunchAgent {label!r} on this bot is not claimed by "
                    f"any app's scheduled_actions[]"
                ),
                evidence={
                    "label": label,
                    "plist_path": plist_path,
                    "bot_id": bot_id,
                },
            ))

    # 2) Evolve-managed sections in workspace HEARTBEAT.md / AGENTS.md
    # that no manifest claims via ``installed_artifact``. v17 replaces
    # the prior openclaw.json#hooks check (the surface didn't exist).
    workspace: Path = ctx["workspace"]
    for md_file in ("HEARTBEAT.md", "AGENTS.md"):
        target = workspace / md_file
        if not target.exists():
            continue
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for anchor in _enumerate_managed_section_anchors(text):
            artifact = f"{md_file}#{anchor}"
            if artifact in claimed:
                continue
            # The marker carries `pkg=…` for attribution; we report the
            # raw anchor so the operator can read it in the file.
            findings.append(Finding(
                assertion_id="scheduled_action_orphan_install",
                severity=SEVERITY_MINOR,
                summary=(
                    f"evolve-managed section {anchor!r} in {md_file} "
                    f"is not claimed by any app's scheduled_actions[]"
                ),
                evidence={
                    "file": md_file,
                    "anchor": anchor,
                    "artifact": artifact,
                    "bot_id": bot_id,
                },
            ))
    return findings


# Heading line — used to enumerate sections in a markdown file. Only
# ``##``+ headings are considered (the top-level ``#`` is reserved for
# the file's title).
_HEADING_RE = re.compile(r"^(?P<level>#{2,4})\s+(?P<anchor>.+?)\s*$", re.MULTILINE)


def _enumerate_managed_section_anchors(text: str) -> list[str]:
    """Return the anchor text of every evolve-managed section in ``text``.

    A section is "managed" when its body (between its heading and the next
    heading) contains an ``<!-- evolve-managed -->`` marker. The returned
    anchors are the heading text (no leading ``#``s), suitable for
    ``{file}#{anchor}`` artifact comparison.

    Top-level ``#`` headings are not considered managed even when followed
    by a marker — the marker is owned by the SECTION it lives in, which
    is the deepest enclosing ``##`` (or deeper) heading.
    """
    matches = list(_HEADING_RE.finditer(text))
    anchors: list[str] = []
    for i, m in enumerate(matches):
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start: body_end]
        if _MANAGED_MARKER_RE.search(body):
            anchors.append(m.group("anchor").strip())
    return anchors


def check_section_drift(manifest: dict, ctx: dict) -> list[Finding]:
    """Section-content sha drift for each scheduled action's cited section.

    Compares the stored `section_sha256` against the current sha of the
    section content extracted at audit time. Drift over the threshold
    fires a `minor` finding nudging the operator to re-scan — the
    section may have legitimately changed, so this is just a hint, not
    a structural break.
    """
    findings: list[Finding] = []
    workspace: Path = ctx["workspace"]
    for action in manifest.get("scheduled_actions") or []:
        if not isinstance(action, dict):
            continue
        trigger = action.get("trigger") or {}
        path = (trigger.get("evidence_path") or "").lstrip("/")
        locator = (trigger.get("evidence_locator") or "").strip()
        expected_sha = (trigger.get("section_sha256") or "").strip().lower()
        action_id = action.get("id") or "?"
        if not path or not locator or not expected_sha:
            continue
        full = workspace / path
        if not full.exists():
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        section_text = _extract_section(text, locator)
        if section_text is None:
            continue   # anchor missing — owned by check_scheduled_action_anchors
        actual_sha = hashlib.sha256(section_text.encode("utf-8")).hexdigest()
        if actual_sha == expected_sha:
            continue
        # Compute fraction of bytes that differ — coarse comparison via
        # length delta + a quick char-set overlap. Avoids pulling in
        # difflib for hot-path; we just want "is this small or big drift".
        drift_fraction = _byte_drift_fraction(section_text.encode("utf-8"), expected_sha, actual_sha)
        if drift_fraction >= _SECTION_DRIFT_THRESHOLD:
            findings.append(Finding(
                assertion_id="scheduled_action_section_drift",
                severity=SEVERITY_MINOR,
                summary=(
                    f"section for scheduled action {action_id!r} in "
                    f"{path!r} has drifted substantially — consider "
                    f"re-running the scanner"
                ),
                evidence={
                    "path": path,
                    "action_id": action_id,
                    "expected_sha": expected_sha,
                    "actual_sha": actual_sha,
                    "drift_fraction": round(drift_fraction, 2),
                },
            ))
    return findings


# ── Section / anchor helpers ─────────────────────────────────────────────────


_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def _anchor_present(text: str, locator: str) -> bool:
    """Return True if the locator resolves to a heading or unique phrase.

    The locator is either:
      - the heading text of a markdown section (e.g. "Daily routines"), or
      - a 5-10-word unique phrase from the behavior body.

    Both are looked up case-insensitively. Whitespace is collapsed on both
    sides so minor reformatting doesn't trip the check.
    """
    norm_text = _collapse_ws(text).lower()
    norm_loc = _collapse_ws(locator).lower()
    if not norm_loc:
        return False
    return norm_loc in norm_text


def _extract_section(text: str, heading_text: str) -> str | None:
    """Return the text of the markdown section under ``heading_text``.

    The section runs from the heading line to the next same-or-higher-level
    heading. If the locator isn't a heading, returns None (drift check
    only runs against heading-anchored sections).
    """
    norm_target = _collapse_ws(heading_text).lower()
    lines = text.splitlines()
    start = -1
    start_level = 0
    for i, line in enumerate(lines):
        m = _MD_HEADING_RE.match(line)
        if not m:
            continue
        h_text = _collapse_ws(m.group(2)).lower()
        if h_text == norm_target:
            start = i
            start_level = len(m.group(1))
            break
    if start < 0:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        m = _MD_HEADING_RE.match(lines[j])
        if m and len(m.group(1)) <= start_level:
            end = j
            break
    return "\n".join(lines[start:end])


def _collapse_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _byte_drift_fraction(current_bytes: bytes, expected_sha: str, actual_sha: str) -> float:
    """Approximate byte-drift fraction.

    sha256 alone tells us "different," not "how different." Since we don't
    have the original content (only its sha), we estimate drift as 1.0
    when the shas differ — i.e. any sha drift is treated as "significant"
    above the threshold. A better estimate would require storing the old
    content too; we trade memory for precision and accept that the
    `minor` severity dampens the noise.
    """
    if expected_sha == actual_sha:
        return 0.0
    # Any sha mismatch counts as full drift in this v1 implementation.
    # Tier 2 owners can tighten this if the `minor` noise becomes a problem.
    return 1.0


# ── v20 — openclaw cron run-status assertion ─────────────────────────────────
#
# Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §17.3 + Q32.
#
# Walks every cron the manifest claims and asks "is the cron's most-recent run
# healthy?" — distinct from the existing check_crons_installed which only
# verifies the schedule is registered. Validation against the production pod
# (personal-bot, team-bot-a, team-bot-c, security-bot, evolve) revealed 14 of 16 scheduled jobs in
# error/skipped state. Operators don't see these failures because the failure
# mode is asymmetric: the cron fires, the work nominally runs, but a side
# channel (delivery, auth, message routing) breaks.

# Phrases that indicate a side-channel failure even when the run's headline
# Status is "ok". Drawn from real openclaw cron run records on the production
# pod (e.g., team-bot-a-task-worker's *"Slack API credentials aren't configured in
# the fallback messenger"* incident). Case-insensitive substring match.
_OC_CRON_DELIVERY_FAILURE_PATTERNS = (
    "message failed",
    "delivery attempted",
    "auth configuration",
    "no route",
    "fail-closed",
    "deliver failed",
    "credentials are missing",
    "credentials aren't configured",
    "credentials not configured",
    "could not deliver",
    "failed to deliver",
    "failed to send",
    "fallback messenger",
)

# How many expected intervals can elapse before a run with Status "skipped"
# is flagged. 1 = next-tick-overdue; 2 = a tick-and-a-half ago; we use 2 so
# transient mid-tick races don't fire false positives.
_OC_CRON_SKIPPED_INTERVAL_TOLERANCE = 2


def _match_manifest_cron_to_live(manifest_cron: dict, live_crons: list[dict]) -> dict | None:
    """Resolve a manifest crons[] entry to its live ``openclaw cron list`` entry.

    Manifest entries carry a ``label`` (e.g. "personal-bot-backup") and a ``script``
    path; live entries carry ``name`` and ``command``. Primary match: name ==
    label. Fallback: script token appears in command. Returns the first live
    entry that matches, or None.
    """
    label = (manifest_cron.get("label") or "").strip()
    script = (_cron_script(manifest_cron) or "").strip()
    if not label and not script:
        return None
    for live in live_crons or []:
        if not isinstance(live, dict):
            continue
        if label and (live.get("name") or "").strip() == label:
            return live
        if script:
            cmd = (live.get("command") or "")
            if script in cmd:
                return live
            # Allow basename match for scripts referenced by full path
            from os.path import basename
            script_base = basename(script)
            if script_base and script_base in cmd:
                return live
    return None


def _last_run_summary(run_history: list[dict]) -> str:
    """Best-effort: pull the summary/error text from the most recent run."""
    if not run_history:
        return ""
    last = run_history[0] if isinstance(run_history, list) else {}
    # Real openclaw cron runs carry both `summary` (free-form, may contain
    # the LLM-generated explanation) and `error` (short failure string).
    # Either may carry the failure-language we're matching against.
    parts = []
    for k in ("summary", "error"):
        v = last.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v)
    return " ".join(parts)


def _last_run_status(run_history: list[dict]) -> str:
    """Headline Status of the most recent run. Returns "" if no runs."""
    if not run_history:
        return ""
    last = run_history[0] if isinstance(run_history, list) else {}
    return (last.get("status") or "").strip().lower()


def check_openclaw_cron_run_status(manifest: dict, ctx: dict) -> list[Finding]:
    """Every claimed cron's most-recent run is healthy.

    For each ``manifest.crons[*]`` entry that matches an openclaw cron in
    ``ctx["openclaw_crons"]``, classify the most recent run from
    ``ctx["openclaw_run_history_by_id"][job_id]``:

    * Status == "error"  → critical Finding. The cron itself reports
      failure on its last invocation. Validation against the production
      pod found this state across team-bot-a-task-worker (Slack credentials
      missing), security-bot-weekly-self-audit, ping-calendar-monitor, and
      maintenance-weekly-report.

    * Status == "skipped" AND elapsed > 2× expected interval  → major
      Finding. The schedule fires but the work defers. personal-bot's 5 openclaw
      crons all sit in this state — schedule registered, never actually
      runs. Sometimes intentional ("delivery=not requested" pattern when
      the operator hasn't subscribed a destination), but always worth
      surfacing because it means the cron contributes nothing.

    * Status == "ok" AND summary contains a failure-language pattern
      → major Finding. The asymmetric-failure case: the cron's headline
      claims success but the LLM-generated summary describes a side-
      channel failure (delivery broken, auth missing, route doesn't
      resolve). team-bot-a-task-worker's *"Slack API credentials aren't
      configured in the fallback messenger"* is the canonical example.

    Silently skips manifest crons that don't match any live entry (the
    cron might be managed by crontab/launchd, which other assertions
    cover) and runs with no recorded history (the cron is registered
    but has never fired — typically because it was just installed and
    its first run is pending).

    Reads from ctx:
        openclaw_crons               — list[dict] from oc_cron_list()
        openclaw_run_history_by_id   — dict[job_id, list[run_dict]]
                                       runs ordered most-recent-first

    The runner is responsible for pre-fetching these so the assertion
    doesn't subprocess per call. When the keys are absent (e.g. the
    runner couldn't reach OC CLI), the assertion returns no findings
    rather than firing false positives.
    """
    findings: list[Finding] = []
    live_crons = ctx.get("openclaw_crons")
    run_history_by_id = ctx.get("openclaw_run_history_by_id") or {}

    if live_crons is None:
        # OC CLI not available this run — don't fire false positives.
        return findings

    for entry in manifest.get("crons") or []:
        if not isinstance(entry, dict):
            continue
        live = _match_manifest_cron_to_live(entry, live_crons)
        if live is None:
            continue
        job_id = live.get("id") or live.get("job_id") or ""
        if not job_id:
            continue
        run_history = run_history_by_id.get(job_id) or []
        if not run_history:
            # No recorded runs yet (likely just installed) — not a finding.
            continue

        status = _last_run_status(run_history)
        summary = _last_run_summary(run_history)

        # Critical: explicit error status on most recent run.
        if status == "error":
            findings.append(Finding(
                assertion_id="openclaw_cron_error",
                severity=SEVERITY_CRITICAL,
                summary=(
                    f"openclaw cron {live.get('name', job_id)!r} last run "
                    f"reported error: {summary[:200]}"
                ),
                evidence={
                    "openclaw_cron_id": job_id,
                    "openclaw_cron_name": live.get("name"),
                    "schedule": live.get("schedule") or _cron_schedule(entry),
                    "command": live.get("command"),
                    "last_summary": summary,
                },
            ))
            continue  # don't double-fire delivery-failure for error rows

        # Major: skipped repeatedly when the schedule expects to fire.
        if status == "skipped":
            findings.append(Finding(
                assertion_id="openclaw_cron_skipped",
                severity=SEVERITY_MAJOR,
                summary=(
                    f"openclaw cron {live.get('name', job_id)!r} last run was "
                    f"skipped — schedule fires but work defers"
                ),
                evidence={
                    "openclaw_cron_id": job_id,
                    "openclaw_cron_name": live.get("name"),
                    "schedule": live.get("schedule") or _cron_schedule(entry),
                    "command": live.get("command"),
                    "last_summary": summary,
                    "delivery": live.get("delivery"),
                },
            ))
            continue

        # Major: ok status but summary contains failure-language. The
        # asymmetric "work succeeded, delivery failed" pattern. This is
        # the single most-leveraged check — it caught team-bot-a's team-bot-a-task-worker
        # which has been failing for an unknown duration with no operator
        # visibility.
        if status == "ok" and summary:
            lower = summary.lower()
            matched = next(
                (p for p in _OC_CRON_DELIVERY_FAILURE_PATTERNS if p in lower),
                None,
            )
            if matched:
                findings.append(Finding(
                    assertion_id="openclaw_cron_delivery_failure",
                    severity=SEVERITY_MAJOR,
                    summary=(
                        f"openclaw cron {live.get('name', job_id)!r} reports "
                        f"Status=ok but summary indicates delivery failure "
                        f"({matched!r}): {summary[:200]}"
                    ),
                    evidence={
                        "openclaw_cron_id": job_id,
                        "openclaw_cron_name": live.get("name"),
                        "schedule": live.get("schedule") or _cron_schedule(entry),
                        "command": live.get("command"),
                        "matched_pattern": matched,
                        "last_summary": summary,
                    },
                ))

    return findings


# ── v21 — discoverability ───────────────────────────────────────────────────
#
# Minimum routing surface a manifest must expose so the bot's LLM (reading
# INSTALLED_APPS.md at session start) can recognize a user's intent and call
# the app. The contract here mirrors app_registry.render_installed_apps_md
# exactly — if a field would render to empty in that document, this check
# fires. Keeping the two in sync is the whole point: audit failures here =
# the renderer would emit a thin entry.

# Minimum hint-word count for user-routed apps. The renderer caps at 12; this
# floor is "enough words that at least one is likely to appear in a typical
# user message." Tuned against the existing pod's manifests — apps with <3
# hints consistently miss user routing.
_DISCOVERABILITY_MIN_HINT_WORDS = 3

# usage.model values for which user-routing fields (hint_words, example_triggers,
# cli) are load-bearing. Scheduled / event-driven apps don't need them — the
# bot relays their output rather than invoking them on user intent.
_USER_ROUTED_MODELS = frozenset({"user-initiated", "ambient", ""})


def _manifest_usage_block(manifest: dict) -> dict:
    """Extract the usage block, tolerating both top-level + identity-nested
    placement (mirrors app_registry._usage_block)."""
    top = manifest.get("usage")
    if isinstance(top, dict):
        return top
    identity = manifest.get("identity")
    if isinstance(identity, dict):
        nested = identity.get("usage")
        if isinstance(nested, dict):
            return nested
    return {}


def _manifest_hint_words(manifest: dict) -> list[str]:
    """Mirror app_registry._hint_words on the raw dict. Returns the union of
    explicit hint_words + capability_tags + session_keywords, deduped."""
    usage = _manifest_usage_block(manifest)
    tr = usage.get("trigger_recognition") or {}
    explicit = tr.get("hint_words")
    out: list[str] = []
    if isinstance(explicit, list):
        for w in explicit:
            if isinstance(w, str) and w.strip() and w.strip() not in out:
                out.append(w.strip())
    # Fall back to capability_tags + session_keywords — the renderer treats
    # these as hint_words when explicit hints are absent. We count both
    # toward the floor since either is what reaches the LLM.
    for field_name in ("capability_tags", "session_keywords"):
        for w in manifest.get(field_name) or []:
            if isinstance(w, str) and w.strip() and w.strip() not in out:
                out.append(w.strip())
    return out


def _manifest_cli_commands(manifest: dict) -> list[str]:
    """Renderer-equivalent CLI command list — non-empty `command` strings."""
    contract = manifest.get("interface_contract") or {}
    cli = contract.get("cli") or []
    out: list[str] = []
    for entry in cli:
        if isinstance(entry, dict):
            cmd = (entry.get("command") or "").strip()
            if cmd:
                out.append(cmd)
    return out


def check_discoverability(manifest: dict, ctx: dict) -> list[Finding]:
    """Manifest exposes enough surface for the bot's LLM to route to it.

    The bot reads INSTALLED_APPS.md at session start; that document is
    rendered from a fixed set of manifest fields. If those fields are empty,
    the app is structurally installed but conversationally invisible — the
    LLM has no way to know when/how to invoke it.

    Routing-only fields (hint_words, example_triggers, CLI) are skipped for
    apps whose ``usage.model`` is ``scheduled`` or ``event-driven`` — those
    apps don't need user-routing surface.
    """
    findings: list[Finding] = []
    usage = _manifest_usage_block(manifest)
    model = str(usage.get("model") or "").strip().lower()

    # 1. usage.model — minor finding; not load-bearing on its own, but its
    #    absence means the routing-only checks below have to assume the
    #    permissive "user-routed" interpretation, and the renderer can't
    #    show a "When to invoke" line.
    if not model:
        findings.append(Finding(
            assertion_id="app_discoverability_no_invocation_model",
            severity=SEVERITY_MINOR,
            summary="manifest.usage.model not set — bot can't tell when to invoke",
            evidence={"field": "usage.model"},
        ))

    # 2. how_to_use — minor; the renderer falls back to description /
    #    identity.purpose, so this is rarely fatal. But if all three are
    #    empty, the LLM has nothing to read about what the app does.
    ht = (usage.get("how_to_use") or "").strip() if usage else ""
    desc = (manifest.get("description") or "").strip()
    identity = manifest.get("identity") or {}
    purpose = (identity.get("purpose") or "").strip() if isinstance(identity, dict) else ""
    if not ht and not desc and not purpose:
        findings.append(Finding(
            assertion_id="app_discoverability_no_how_to_use",
            severity=SEVERITY_MAJOR,
            summary=(
                "no usage.how_to_use, description, or identity.purpose — "
                "LLM has no prose describing what this app is for"
            ),
            evidence={"field": "usage.how_to_use"},
        ))

    # Routing-only checks below only apply to user-routed apps. Scheduled /
    # event-driven apps run on their own trigger; the bot relays their output
    # rather than recognizing user intent.
    is_user_routed = model in _USER_ROUTED_MODELS
    if not is_user_routed:
        return findings

    # 3. hint_words — major; without enough hint words, the renderer's
    #    "**Hint words to recognize in user messages:** …" line is empty or
    #    short, and routing degrades to LLM intuition.
    hints = _manifest_hint_words(manifest)
    if len(hints) < _DISCOVERABILITY_MIN_HINT_WORDS:
        findings.append(Finding(
            assertion_id="app_discoverability_thin_hint_words",
            severity=SEVERITY_MAJOR,
            summary=(
                f"only {len(hints)} hint word(s) across usage.trigger_recognition.hint_words "
                f"+ capability_tags + session_keywords (need at least "
                f"{_DISCOVERABILITY_MIN_HINT_WORDS})"
            ),
            evidence={
                "field": "usage.trigger_recognition.hint_words",
                "count": len(hints),
                "minimum": _DISCOVERABILITY_MIN_HINT_WORDS,
                "found": hints[:12],
            },
        ))

    # 4. example_triggers — major; the renderer's "Example user messages
    #    that should route here" list is empty without these, so the LLM
    #    has no pattern-match examples for intent recognition.
    triggers = manifest.get("example_triggers") or []
    valid_triggers = [t for t in triggers if isinstance(t, str) and t.strip()]
    if not valid_triggers:
        findings.append(Finding(
            assertion_id="app_discoverability_no_example_triggers",
            severity=SEVERITY_MAJOR,
            summary="no manifest.example_triggers — LLM has no user-message examples to match against",
            evidence={"field": "example_triggers"},
        ))

    # 5. interface_contract.cli — major; without at least one CLI command,
    #    the renderer's "How to invoke (under the hood)" section is empty
    #    and the LLM has no command to call. Apps with no CLI are usually
    #    scheduled — which was caught above — so reaching here implies
    #    user-routed-without-an-entrypoint.
    #
    #    Exception: meta-installer manifests (app_dependencies-only, no
    #    files, no scheduled_actions) have no CLI by design — their whole
    #    purpose is to package dependent apps for one-shot install (e.g.
    #    EA Pack). Skip the no_cli check for those; the rest of the
    #    discoverability surface still applies so the LLM can answer
    #    "what does this bundle include?" type questions.
    if not _looks_like_meta_installer(manifest):
        cli_commands = _manifest_cli_commands(manifest)
        if not cli_commands:
            findings.append(Finding(
                assertion_id="app_discoverability_no_cli",
                severity=SEVERITY_MAJOR,
                summary=(
                    "no interface_contract.cli[].command — user-routed app has no "
                    "invocation surface"
                ),
                evidence={"field": "interface_contract.cli"},
            ))

    return findings


def _looks_like_meta_installer(manifest: dict) -> bool:
    """A meta-installer manifest is one whose entire purpose is to
    declare ``app_dependencies`` so a gallery install pulls in N child
    apps; it has no code of its own. Structural signature:
    ``app_dependencies`` non-empty AND ``files`` empty AND
    ``scheduled_actions`` empty AND ``event_triggers`` empty.

    Used by the discoverability check to skip the no_cli finding —
    meta-installers legitimately have no CLI surface. The rest of the
    discoverability checks still apply (the bot's LLM should be able
    to discuss what's in the bundle), so this is only about the CLI
    field specifically.
    """
    has_deps = bool(manifest.get("app_dependencies"))
    has_files = bool(manifest.get("files"))
    has_actions = bool(manifest.get("scheduled_actions"))
    has_events = bool(manifest.get("event_triggers"))
    return has_deps and not has_files and not has_actions and not has_events


# ── v22: bootstrap-cost discipline ──────────────────────────────────────────
#
# Thresholds match app_bootstrap_footprint constants; importers depend on
# these being the same number. If we promote the per-app gate to "warn"
# severity later, the threshold lives there too — don't copy it into a
# runner config.

_BOT_GUIDANCE_BYTES_LIMIT = 1024


def _bot_guidance_byte_count(manifest: dict) -> int:
    """Sum the UTF-8 bytes of the manifest's bot_guidance block.

    bot_guidance is conventionally a list of {audience, text, ...} dicts.
    Some older manifests use a plain string; some forge versions emit a
    raw string under a different key shape. We handle both, and fall
    back to the JSON-encoded form for unknown shapes — over-count is fine
    here; the goal is to never under-count past the threshold.
    """
    bg = manifest.get("bot_guidance")
    if bg is None:
        return 0
    if isinstance(bg, str):
        return len(bg.encode("utf-8"))
    if isinstance(bg, list):
        total = 0
        for entry in bg:
            if isinstance(entry, dict) and isinstance(entry.get("text"), str):
                total += len(entry["text"].encode("utf-8"))
            else:
                total += len(json.dumps(entry).encode("utf-8"))
        return total
    return len(json.dumps(bg).encode("utf-8"))


def _manifest_declares_llm_intent(manifest: dict) -> bool:
    """True if the manifest declares any LLM-bearing work via recursive_llm.

    A non-empty `recursive_llm.purposes` list signals the app expects to
    make at least one LLM call. Apps without it can run as pure crons
    (no LLM, no per-turn bot context) — that's what the cron-eligible
    check is enforcing.
    """
    rl = manifest.get("recursive_llm")
    if not isinstance(rl, dict):
        return False
    purposes = rl.get("purposes")
    return bool(purposes)


def _manifest_has_cli(manifest: dict) -> bool:
    """True if the manifest declares any CLI commands.

    Reuses the existing _manifest_cli_commands helper used by the
    discoverability check — single source of truth for "what's an
    invocable surface."
    """
    return bool(_manifest_cli_commands(manifest))


def _has_script_realized_file(manifest: dict) -> bool:
    """True if ``realized_files[]`` carries at least one script entry.

    A script (.py / .sh / .bash) in realized_files[] is an operator-
    invocable surface — the operator runs ``python scripts/foo.py``
    or similar. The fact that interface_contract.cli isn't explicitly
    populated is a manifest-completeness issue, not a "no surface"
    condition.

    Pod survey 2026-06-09 found ~10–15 of the firing
    app_no_producer_surface alerts target Instances whose only
    invocation evidence is a script in realized_files (typical shape:
    a ``scripts/<name>.py`` file with no scheduled_actions, no
    heartbeat, no cron). The scanner-side pass synthesizes
    ``interface_contract.cli`` from these scripts so the field is
    populated on the next scan; this verifier-side clause is the
    safety net for manifests where that synthesis hasn't run yet.
    See docs/alert-root-cause-audit-2026-06-09.md.

    Mirrored in scanner._has_script_realized_file — kept in two
    places to avoid the cross-package import.
    """
    for rf in manifest.get("realized_files") or []:
        if not isinstance(rf, dict):
            continue
        path = (rf.get("path") or "").lower()
        if path.endswith((".py", ".sh", ".bash")):
            return True
    return False


def check_bot_guidance_size(manifest: dict, ctx: dict) -> list[Finding]:
    """`bot_guidance` adds to every turn's system prompt. Cap each app's
    contribution so the per-bot baseline doesn't grow unbounded as apps
    install.

    Calibration phase: SEVERITY_INFO. See principle-apps-minimize-bootstrap-cost.
    """
    bg_bytes = _bot_guidance_byte_count(manifest)
    if bg_bytes <= _BOT_GUIDANCE_BYTES_LIMIT:
        return []
    return [Finding(
        assertion_id="app_bot_guidance_oversized",
        severity=SEVERITY_INFO,
        summary=(
            f"bot_guidance is {bg_bytes} bytes (limit {_BOT_GUIDANCE_BYTES_LIMIT}) — "
            f"every turn this bot runs pays the difference"
        ),
        evidence={
            "field": "bot_guidance",
            "bytes": bg_bytes,
            "limit": _BOT_GUIDANCE_BYTES_LIMIT,
        },
    )]


def check_invocation_mode_subagent(manifest: dict, ctx: dict) -> list[Finding]:
    """Apps with CLI invocation surfaces that ALSO declare LLM intent
    should route through a subagent — a clean narrow context — instead
    of riding the bot's main session.

    Composes with the agent-freelance-bypass class: a CLI script that
    enters the main session inherits everything the main session accrued,
    can freelance on general tools, and the bot's per-turn baseline pays
    the inherited weight.

    Skipped for scheduled / event-driven apps (those don't get user-
    initiated CLI invocations) and for manifests with no LLM intent
    (cron-eligible — different check fires).
    """
    if not _manifest_declares_llm_intent(manifest):
        return []
    if not _manifest_has_cli(manifest):
        return []

    usage = _manifest_usage_block(manifest)
    model = str(usage.get("model") or "").strip().lower()
    if model and model not in _USER_ROUTED_MODELS:
        return []

    mode = str(manifest.get("invocation_mode") or "").strip().lower()
    if mode == "subagent":
        return []

    return [Finding(
        assertion_id="app_invocation_mode_not_subagent",
        severity=SEVERITY_INFO,
        summary=(
            f"invocation_mode={mode or 'unset'!r}; user-routed app with LLM intent should "
            f"declare invocation_mode='subagent' so its LLM call runs on a clean context"
        ),
        evidence={
            "field": "invocation_mode",
            "current": mode or None,
            "expected": "subagent",
        },
    )]


# ── v23: producer surface presence (companion to PR #2476) ──────────────────


def producer_surface_kinds(manifest: dict) -> list[str]:
    """Return the populated producer-surface kinds on this manifest.

    A producer surface is something that DRIVES the app — concrete
    machinery the bot or pod can invoke. Hint words and bot_guidance are
    NOT producer surfaces on their own: they tell the renderer "show this
    app to the LLM", but if nothing on the manifest is invocable from
    that recognition, the app is still a shell. They become full surfaces
    when paired with a CLI or scheduled action below.

    Order matches the manifest schema doc in
    packages/admin/evolve_admin/applications/manifest.py:
      - scheduled_actions[] with at least one non-vacuous install
      - heartbeat_evidence with section_anchors
      - cron_evidence with labels
      - crons[] non-empty
      - event_triggers[] non-empty
      - interface_contract.cli[] non-empty
      - realized_files[] containing a script (.py / .sh / .bash)

    v23.1: scheduled_actions[] counts only when at least one entry has
    install.file ∪ install.plist_label ∪ install.command non-empty. Pre-
    v23.1 the scanner minted stub entries with ``mechanism: "unknown"``
    and entirely-None install blocks; treating those as surfaces caused
    systematic underfire of app_no_producer_surface. Pod survey
    2026-06-09: 29 of 37 manifests had at least one vacuous entry; in
    10 cases the vacuous entries were the ONLY scheduled_actions present.

    v23.2 (2026-06-09): a script entry in realized_files[] counts as an
    inferred CLI surface. Closes the gap where v7-arc Instance migration
    dropped interface_contract but kept realized_files — the app is
    runnable by hand even though the contract field is empty.
    """
    kinds: list[str] = []

    for sa in manifest.get("scheduled_actions") or []:
        if not isinstance(sa, dict):
            continue
        install = sa.get("install") or {}
        if isinstance(install, dict) and (
            install.get("file")
            or install.get("plist_label")
            or install.get("command")
        ):
            kinds.append("scheduled_actions")
            break

    hb = manifest.get("heartbeat_evidence") or {}
    if isinstance(hb, dict) and hb.get("section_anchors"):
        kinds.append("heartbeat_evidence")

    ce = manifest.get("cron_evidence") or {}
    if isinstance(ce, dict) and ce.get("labels"):
        kinds.append("cron_evidence")

    if manifest.get("crons"):
        kinds.append("crons")

    if manifest.get("event_triggers"):
        kinds.append("event_triggers")

    if _manifest_has_cli(manifest):
        kinds.append("interface_contract.cli")

    if _has_script_realized_file(manifest):
        kinds.append("realized_files.script")

    return kinds


def _is_scanner_shell(manifest: dict) -> bool:
    """True when the manifest is a scanner-minted shell with nothing concrete.

    Two shapes qualify:

    1. Legacy v6 wrapped manifest: ``instance.realized_files == []`` AND
       ``source_detail.startswith("scan:")``.
    2. v7-arc Instance observationally minted by the scanner: top-level
       ``manifest_shape == "v7-arc"`` AND
       ``provenance.manifest_origin == "observational"`` AND
       (top-level ``realized_files`` is empty or missing). This is the
       team-bot-a-style shape surveyed pod-wide 2026-06-09: 5 Instances on team-bot-a
       with ``spec_id: None``, ``instance_id: None``, scanner-stamped
       observational provenance — the scanner inferred an app name from
       conversation/docs but never bound it to a Spec or realized files.

    In either state the scanner inferred an app name but couldn't
    attribute any files to it, so "no producer surface" tells the
    operator nothing actionable — the real failure is upstream in the
    scanner. Downgrading these to ``info`` keeps them out of the
    user-facing major lane until the scanner stops minting empty shells.

    Pre-v7 manifests without an ``instance`` block AND without v7-arc
    markers are treated as non-shells (we have no realized_files
    signal for them).
    """
    # Shape 2: v7-arc observational Instance with no realized files.
    if manifest.get("manifest_shape") == "v7-arc":
        provenance = manifest.get("provenance") or {}
        if isinstance(provenance, dict) and (
            provenance.get("manifest_origin") == "observational"
            or provenance.get("created_by") == "scanner"
        ):
            realized = manifest.get("realized_files")
            if not isinstance(realized, list) or not realized:
                return True

    # Shape 1: legacy v6 wrapped manifest.
    inst = manifest.get("instance")
    if not isinstance(inst, dict):
        return False
    realized = inst.get("realized_files")
    if not isinstance(realized, list) or realized:
        return False
    detail = str(manifest.get("source_detail") or "")
    return detail.startswith("scan:")


def _is_observational_instance(manifest: dict) -> bool:
    """True for a v7-arc Instance the scanner minted by OBSERVATION rather
    than installed from a forge/gallery Spec or produced by migration.

    Signature: ``manifest_shape == "v7-arc"`` AND
    (``provenance.manifest_origin == "observational"`` OR
    ``provenance.created_by == "scanner"``).

    Such an Instance is the scanner's *description of how a bot already
    uses a set of files* — a logger, tracker, memory store, workspace —
    not a *designed app with a delivery obligation*. It was never built
    with producer machinery, so "no producer surface" is inherent to it,
    not a defect. This is the broader sibling of ``_is_scanner_shell``
    Shape 2, but WITHOUT the realized-files-empty requirement: an
    observed app with attributed data files is even more clearly a
    working passive store than one with none.
    """
    if manifest.get("manifest_shape") != "v7-arc":
        return False
    prov = manifest.get("provenance") or {}
    if not isinstance(prov, dict):
        return False
    return (
        prov.get("manifest_origin") == "observational"
        or prov.get("created_by") == "scanner"
    )


def _has_data_realized_files(manifest: dict) -> bool:
    """True if ``realized_files[]`` carries at least one attributed work
    product the bot maintains — any path that isn't the app's OWN manifest
    json (bookkeeping, not user data).

    Scripts already count as a producer surface upstream
    (``_has_script_realized_file``); this is the *data-substrate* signal
    that an observed app is a real, working store (logger / tracker /
    workspace) rather than a bare scanner-inferred name. A manifest whose
    only realized file is its own ``manifests/<id>.json`` — or whose
    realized files are all other manifests (a scanner-minted "registry"
    artifact) — has no attributed user data and does NOT qualify.
    """
    for rf in manifest.get("realized_files") or []:
        path = rf.get("path") if isinstance(rf, dict) else rf
        if not isinstance(path, str) or not path.strip():
            continue
        p = path.strip()
        if p.startswith("manifests/") and p.endswith(".json"):
            continue
        return True
    return False


def _declares_delivery_intent(manifest: dict) -> bool:
    """True if the manifest explicitly DECLARES it should produce/deliver
    output — a ``delivery_contract`` on any ``scheduled_actions[]`` entry,
    or a non-empty top-level ``outputs[]``.

    These are the schema's obligation-declaration surfaces (manifest v23 /
    spec-proactive-delivery-monitor). When an app DECLARES delivery but
    has no producer surface to fulfill it, the missing surface is a real,
    operator-actionable defect — so the check fires at major even for an
    observational Instance. This is the "supposed to deliver but produces
    nothing" case the signal exists to catch; the passive-app calibration
    below must not suppress it.
    """
    for sa in manifest.get("scheduled_actions") or []:
        if isinstance(sa, dict) and sa.get("delivery_contract"):
            return True
    outputs = manifest.get("outputs")
    return isinstance(outputs, list) and bool(outputs)


def check_app_has_producer_surface(manifest: dict, ctx: dict) -> list[Finding]:
    """Manifest declares at least one concrete input/trigger surface.

    Catches the empty-shell failure mode where a *designed* manifest names
    an app but no machinery — schedule, anchor, cron, event trigger, CLI —
    is declared to invoke it. The Session-Turn-Logs case (PR #2476) is the
    motivating example: name, slug-variant hint_words, no producer.
    The L3 platform-files sweep handles that specific case structurally;
    this check is the general-form audit that catches new variants
    whose file footprint isn't platform-written.

    Skipped for retired/dismissed manifests — their machinery is
    expected to be gone.

    Passive-app calibration (companion to the R1 verifier-noise fix,
    PR #2828). An *observational* scanner-minted Instance
    (``_is_observational_instance``) is the scanner's description of how a
    bot already works with a set of files, NOT a designed app with a
    delivery obligation — so the absence of a producer surface is inherent
    to it, not a defect. Two sub-cases:

      * It has attributed data files (``_has_data_realized_files``) → a
        working passive data store (logger / tracker / workspace). Firing
        here is a false positive — return [] so the Signal sweep-resolves.
        Pod survey 2026-06-13: 13 of 29 firing signals were this shape
        (a Home Repairs Log, a Job Search Tracker, a Work Order Tracker, a
        Heartbeat Monitoring app with 25 data files, …), all at the louder
        major/warn severity despite being healthy passive stores.
      * It has no attributed work (no data files, or only its own manifest
        / a registry of manifests) → the scanner inferred a name but bound
        no work to it. Unactionable for the operator (the fix is upstream
        in the scanner). Downgrade to ``info`` — audit trail, out of the
        alert lane. Subsumes the existing ``_is_scanner_shell`` empty-
        realized case for v7-arc observational instances.

    An observational Instance that DECLARES delivery intent
    (``_declares_delivery_intent`` — a delivery_contract / outputs[]) is
    NOT suppressed: a declared-but-unfulfilled delivery surface is the
    genuine "supposed to deliver but produces nothing" defect this signal
    exists to catch.

    Scanner-shell exception (legacy): a fresh scanner output
    (``source_detail`` starts with ``scan:`` AND ``instance.realized_files``
    is empty) downgrades to ``info`` for the same reason.
    """
    status = str(manifest.get("status") or "").strip().lower()
    if status in {"retired", "dismissed", "archived"}:
        return []

    kinds = producer_surface_kinds(manifest)
    if kinds:
        return []

    observational = _is_observational_instance(manifest)
    declares_delivery = _declares_delivery_intent(manifest)

    # Passive-app FP: observed data store, no declared delivery obligation,
    # real data files attributed. The app works as designed — don't fire.
    if observational and not declares_delivery and _has_data_realized_files(manifest):
        return []

    scanner_shell = _is_scanner_shell(manifest)
    # An observational Instance with no attributed work (no data files, or
    # only manifest bookkeeping) is a scanner-attribution shell too, even
    # when a residual manifest path keeps realized_files technically
    # non-empty so _is_scanner_shell's empty-check misses it.
    observational_shell = (
        observational and not declares_delivery
        and not _has_data_realized_files(manifest)
    )
    info = scanner_shell or observational_shell
    severity = SEVERITY_INFO if info else SEVERITY_MAJOR
    summary = (
        "scanner-discovered Instance with no attributed work product — "
        "the finding tells the operator nothing actionable until the "
        "scanner attributes files (or the Instance is re-Spec'd)"
        if info else
        "no producer surface declared — none of scheduled_actions, "
        "heartbeat_evidence.section_anchors, cron_evidence.labels, "
        "crons, event_triggers, interface_contract.cli, or a script in "
        "realized_files is populated; the manifest names an app but no "
        "machinery can invoke it"
    )

    return [Finding(
        assertion_id="app_no_producer_surface",
        severity=severity,
        summary=summary,
        evidence={
            "fields_checked": [
                "scheduled_actions",
                "heartbeat_evidence.section_anchors",
                "cron_evidence.labels",
                "crons",
                "event_triggers",
                "interface_contract.cli",
                "realized_files.script",
            ],
            "scanner_shell": scanner_shell,
            "observational_shell": observational_shell,
            "declares_delivery_intent": declares_delivery,
        },
    )]


def check_v7_arc_instance_has_spec_binding(
    manifest: dict, ctx: dict,
) -> list[Finding]:
    """v7-arc Instances bind to a Spec via provenance.spec_id +
    spec_version. When the binding is broken — null spec_id, or Spec
    file absent at the expected gallery path — hydration silently
    returns the bare Instance and every downstream audit reads the
    Spec-less shape, fanning out unrelated findings (most prominently
    app_no_producer_surface) on what is really one root cause.

    This check surfaces the orphan state directly. Only applies to v7-arc
    Instances; legacy shapes pass without check. Reads
    ``ctx["shared_dir"]`` to confirm the Spec file exists; if shared_dir
    isn't in ctx (older runners), only the spec_id-present branch fires.

    Severity: major. Operator action is real (rebind to a Spec, or
    archive the Instance) — this isn't an info-only signal.
    """
    if manifest.get("manifest_shape") != "v7-arc":
        return []

    status = str(manifest.get("status") or "").strip().lower()
    if status in {"retired", "dismissed", "archived"}:
        return []

    provenance = manifest.get("provenance") or {}
    if not isinstance(provenance, dict):
        provenance = {}
    spec_id = provenance.get("spec_id")
    spec_version = provenance.get("spec_version")

    if not spec_id:
        return [Finding(
            assertion_id="orphan_v7_arc_instance",
            severity=SEVERITY_MAJOR,
            summary=(
                "v7-arc Instance has no provenance.spec_id — hydration "
                "is impossible, so name/description/files all render "
                "blank in the admin UI and every spec-driven audit "
                "fires by default"
            ),
            evidence={
                "manifest_shape": "v7-arc",
                "spec_id": None,
                "spec_version": spec_version,
                "manifest_origin": provenance.get("manifest_origin"),
                "created_by": provenance.get("created_by"),
            },
        )]

    if not spec_version:
        return [Finding(
            assertion_id="orphan_v7_arc_instance",
            severity=SEVERITY_MAJOR,
            summary=(
                "v7-arc Instance has provenance.spec_id but no "
                "spec_version — hydration looks up a versioned file "
                "and silently fails"
            ),
            evidence={
                "manifest_shape": "v7-arc",
                "spec_id": spec_id,
                "spec_version": None,
            },
        )]

    shared_dir = ctx.get("shared_dir")
    if not isinstance(shared_dir, Path):
        return []

    candidates = [
        shared_dir / "gallery" / "local" / spec_id / f"{spec_version}.json",
        shared_dir / "gallery" / "builtin" / spec_id / f"{spec_version}.json",
    ]
    source_pod_id = provenance.get("source_pod_id")
    if source_pod_id:
        candidates.insert(
            1,
            shared_dir / "gallery" / "imported" / source_pod_id / spec_id
            / f"{spec_version}.json",
        )

    if any(p.is_file() for p in candidates):
        return []

    return [Finding(
        assertion_id="orphan_v7_arc_instance",
        severity=SEVERITY_MAJOR,
        summary=(
            f"v7-arc Instance binds to Spec {spec_id}@{spec_version} but "
            f"no Spec file exists at the expected gallery paths — "
            f"hydration returns the bare Instance"
        ),
        evidence={
            "manifest_shape": "v7-arc",
            "spec_id": spec_id,
            "spec_version": spec_version,
            "source_pod_id": source_pod_id,
            "checked_paths": [str(p) for p in candidates],
        },
    )]


def check_cron_eligible_used_heartbeat(manifest: dict, ctx: dict) -> list[Finding]:
    """Apps that declare `heartbeat_evidence` but no LLM intent could
    run as pure crons. Heartbeat-anchoring drags them into every bot
    session's per-turn injection budget for no benefit — the cron path
    is free.

    "No LLM intent" = manifest.recursive_llm.purposes is empty. The
    runtime-bot-transport scan (does any script import bot_tool /
    invoke subagent / shell to openclaw_headless) is out of scope here;
    we trust the manifest declaration.
    """
    hb = manifest.get("heartbeat_evidence") or {}
    if not isinstance(hb, dict) or not hb:
        return []
    if _manifest_declares_llm_intent(manifest):
        return []

    return [Finding(
        assertion_id="app_cron_eligible_used_heartbeat",
        severity=SEVERITY_INFO,
        summary=(
            "declares heartbeat_evidence with no recursive_llm.purposes — "
            "could run as a cron with no per-turn bootstrap cost"
        ),
        evidence={
            "field": "heartbeat_evidence",
            "recursive_llm_purposes_count": 0,
        },
    )]


# Default assertion order. Importers can subset for partial runs. Order
# matters only for the trail readability — assertions don't depend on each
# other's findings.
def _normalize_run_file_path(path: str) -> str:
    """Normalize a run_file evidence path for data_files comparison.

    The contract spells the per-window placeholder ``{date}``; gallery
    ``interface_contract.data_files`` declarations spell it
    ``YYYY-MM-DD``. Both normalize to the same string.
    """
    return path.strip().replace("{date}", "YYYY-MM-DD")


def check_delivery_contract(manifest: dict, ctx: dict) -> list[Finding]:
    """v25 — delivery_contract{} shape + declared evidence paths.

    Spec: spec-proactive-delivery-monitor-2026-06-10.md §5 + §11. Two
    assertions per scheduled_actions[] entry carrying the optional block:

    * ``delivery_contract_invalid`` (major) — the block fails the
      canonical validator. The delivery_monitor falls back to derived
      Option-A defaults for a malformed contract, so the author's
      declared window / evidence / heal assertion silently doesn't
      apply until the shape is fixed — worth a loud finding.
    * ``delivery_contract_evidence_undeclared`` (minor) — declared
      ``run_file`` evidence whose path doesn't appear in
      ``interface_contract.data_files``. §5.4: the run-record contract
      is enforced only for apps that *declare* run_file evidence; a
      path the app never claims to write can't prove deliveries.

    Per the §11 boundary, this stays static — no per-window timeliness
    checks here; those belong to the delivery_monitor daemon.
    """
    # Canonical validator lives with the schema. The audit venv carries
    # both packages; if the import ever breaks, run_all surfaces it as
    # an assertion_crashed finding rather than silently skipping.
    from evolve_admin.applications.manifest import validate_delivery_contract  # type: ignore

    findings: list[Finding] = []
    data_files = {
        _normalize_run_file_path(str(df.get("path") or ""))
        for df in (manifest.get("interface_contract") or {}).get("data_files") or []
        if isinstance(df, dict)
    }

    for entry in manifest.get("scheduled_actions") or []:
        if not isinstance(entry, dict):
            continue
        contract = entry.get("delivery_contract")
        if contract is None:
            continue
        action_id = entry.get("id") or "<no id>"
        errors = validate_delivery_contract(contract)
        if errors:
            findings.append(Finding(
                assertion_id="delivery_contract_invalid",
                severity=SEVERITY_MAJOR,
                summary=(
                    f"scheduled action {action_id!r} has a malformed "
                    f"delivery_contract ({'; '.join(errors[:3])}) — the "
                    f"delivery monitor is using derived defaults instead"
                ),
                evidence={"action_id": action_id, "errors": errors},
            ))
            continue
        for role in ("ran", "delivered"):
            evidence = (contract.get("evidence") or {}).get(role) or {}
            if evidence.get("kind") != "run_file":
                continue
            normalized = _normalize_run_file_path(str(evidence.get("path") or ""))
            if normalized not in data_files:
                findings.append(Finding(
                    assertion_id="delivery_contract_evidence_undeclared",
                    severity=SEVERITY_MINOR,
                    summary=(
                        f"scheduled action {action_id!r} declares run_file "
                        f"delivery evidence at {normalized!r} but "
                        f"interface_contract.data_files doesn't list it"
                    ),
                    evidence={
                        "action_id": action_id,
                        "evidence_role": role,
                        "path": normalized,
                        "declared_data_files": sorted(data_files),
                    },
                ))
    return findings


DEFAULT_ASSERTIONS = (
    check_files_exist,
    check_files_sha,
    check_cron_scripts_exist,
    check_cron_schedules,
    check_crons_installed,
    # check_test_command removed 2026-06-08 — app-test surface killed.
    check_python_packages,
    # v13 — scheduled-action contracts
    check_scheduled_action_evidence_paths,
    check_scheduled_action_anchors,
    check_scheduled_action_inputs,
    check_heartbeat_anchors,
    check_cron_labels_loaded,
    check_section_drift,
    # v16 install-site verification (spec-forge-side-effects-2026-06-02 §7)
    check_scheduled_action_install_present,         # A1
    check_scheduled_action_command_resolvable,      # A2
    check_scheduled_action_output_channel,          # A5
    check_orphan_install_artifacts,                 # A6
    # v20 — openclaw cron run-status (spec-app-coherence-...-2026-06-05 §17.3 + Q32)
    check_openclaw_cron_run_status,
    # v21 — discoverability (whether bot LLM can route to the installed app)
    check_discoverability,
    # v22 — bootstrap-cost discipline (info-only calibration phase).
    # See principle-apps-minimize-bootstrap-cost.md.
    check_bot_guidance_size,
    check_invocation_mode_subagent,
    check_cron_eligible_used_heartbeat,
    # v23 — producer surface presence (companion to PR #2476).
    check_app_has_producer_surface,
    # v24 — v7-arc Instance → Spec binding integrity.
    check_v7_arc_instance_has_spec_binding,
    # v25 — manifest-v23 delivery_contract{} shape + evidence declaration
    # (spec-proactive-delivery-monitor-2026-06-10.md §5 + §11).
    check_delivery_contract,
)


def run_all(
    manifest: dict, ctx: dict, assertions: tuple = DEFAULT_ASSERTIONS,
) -> list[Finding]:
    """Run every assertion against the manifest and return concatenated findings.

    Each assertion is wrapped so an unexpected exception from one doesn't
    abort the rest — a broken assertion lands as an ``info`` finding and the
    audit continues.
    """
    out: list[Finding] = []
    for fn in assertions:
        try:
            out.extend(fn(manifest, ctx))
        except Exception as exc:
            out.append(Finding(
                assertion_id="assertion_crashed",
                severity=SEVERITY_INFO,
                summary=f"assertion {fn.__name__} raised {type(exc).__name__}: {exc}",
                evidence={"assertion": fn.__name__, "error": str(exc)},
            ))
    return out


# ── Helpers ─────────────────────────────────────────────────────────────────


def _cron_schedule(entry: Any) -> str:
    if isinstance(entry, str):
        # Raw crontab line — first 5 tokens are the schedule, or @keyword
        s = entry.strip()
        if s.startswith("@"):
            return s.split()[0]
        parts = s.split()
        return " ".join(parts[:5]) if len(parts) >= 6 else ""
    if isinstance(entry, dict):
        return (entry.get("schedule") or "").strip()
    return ""


def _cron_script(entry: Any) -> str:
    if isinstance(entry, str):
        s = entry.strip()
        if s.startswith("@"):
            return s.split(maxsplit=1)[1] if len(s.split(maxsplit=1)) > 1 else ""
        parts = s.split()
        return " ".join(parts[5:]) if len(parts) >= 6 else ""
    if isinstance(entry, dict):
        return (entry.get("script") or entry.get("script_path") or "").strip()
    return ""


def _resolve_script_path(script: str, workspace: Path) -> Path | None:
    """Return the resolved path if the cron script exists, else None.

    Tries: absolute, ${HOME}-relative, workspace-relative. Cron scripts are
    typically full commands like ``python3 ~/.openclaw/workspace/foo.py``;
    we extract the last token ending in .py/.sh as the actual script.
    """
    # Strip leading ``python3``, ``bash``, etc. — take the last *.py / *.sh token
    tokens = script.split()
    target = None
    for t in reversed(tokens):
        # Strip shell quoting
        t = t.strip("'\"")
        if t.endswith((".py", ".sh", ".rb", ".js")):
            target = t
            break
    if target is None:
        target = tokens[-1] if tokens else script
    target = os.path.expanduser(target)
    p = Path(target)
    if p.is_absolute() and p.exists():
        return p
    # Try workspace-relative
    rel = workspace / target.lstrip("/")
    if rel.exists():
        return rel
    return None


def _parse_cron_schedule(schedule: str) -> bool:
    """Lightweight crontab schedule validator.

    Accepts @keyword forms and 5-field minute/hour/dom/month/dow forms.
    Each field allows digits, *, comma, dash, slash. Doesn't validate
    value ranges (cron itself does), only structure.
    """
    s = schedule.strip()
    if not s:
        return False
    if s.startswith("@"):
        return s.split()[0] in _CRON_KEYWORDS
    parts = s.split()
    if len(parts) != 5:
        return False
    return all(_CRON_FIELD_RE.match(p) for p in parts)


def _normalize_cron_line(line: str) -> str:
    return " ".join(line.split()).strip()


def _cron_soft_match(schedule: str, script: str, live_line: str) -> bool:
    """Loose match: schedule prefix + script substring both present in line.

    Real crontab lines are often wrapped with cd/env prefixes
    (``cd $HOME && python3 ~/.openclaw/...``). A strict equality match misses
    these, so we accept any line that starts with the schedule and contains
    the script's basename.
    """
    norm = _normalize_cron_line(live_line)
    if not norm.startswith(schedule):
        return False
    # Match on the script's last path token (basename) to tolerate prefix
    # differences like cd/python3 wrappers
    basename = script.rsplit("/", 1)[-1].split()[0]
    return basename in norm
