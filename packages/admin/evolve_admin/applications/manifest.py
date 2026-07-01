"""
Application manifest schema and registry.

An application manifest is the source of truth for a named set of bot functionality.
It defines goals, success criteria, and privacy constraints — and persists
across sessions so Evolve can measure drift and propose improvements.

Manifests are per-bot state, stored as JSON in:
  /Users/<bot>/.openclaw/workspace/manifests/{application_id}.json
(resolved by applications_dir(); see its docstring)

Schema version 2 adds: purpose, example_triggers, test_cases (flat dicts),
evidence_files (list), tags, improvement_history, updated_at, last_test_result,
satisfaction_notes.

Schema version 3 adds: identity, success_criteria, constraints, satisfaction,
open_questions (4-section RSI fields).

Schema version 4 adds: version, objective, owner, maintainers, files, crons,
inputs, outputs, exported_hooks, test_command, rsigrade_signals, docs,
last_reviewed (operational/registry fields). Also adds: last_test_run,
last_test_exit_code, compliance_suppressed.

Schema version 5 adds: pkg_id, pkg_version, gallery_version, display_name,
author, build_spec, install_job, dependencies (gallery & forge provenance fields).
Also promotes files from list[str] to list[dict] with full provenance records.
Use manifest.file_paths() for backward-compatible path access.

Schema version 6 adds: app_dependencies, requirements, interface_contract.

app_dependencies — app-level package requirements (other gallery apps that must
be installed before this one can be built). Distinct from `dependencies`, which
tracks cross-app file sharing at the file level.
Schema: [{pkg_id, display_name, required: bool, reason}]

requirements — technical prerequisites the bot must satisfy before the app can
run. Checked by the preflight system before forge is queued.
Schema: {
  integrations: [{id, display_name, required, check_path, setup_doc, reason}],
  secrets:      [{key, storage, required, expirable}],
  system:       [{name, check, required}],
  python_packages: [{import, pip_name, required}]
}

interface_contract — stable external interface surfaces. Pre-populated in gallery
manifests with normative intent; overwritten by forge after each build with the
actual field names, CLI signatures, and file paths it chose. Dependent app builds
receive the installed dependency's interface_contract as context.
Schema: {
  populated_by_forge: bool,
  extracted_at: str (ISO),
  data_files: [{path, description, schema: {storage_format, fields: {name: type}}}],
  cli: [{command, key_flags, output_signals}],
  key_paths: {name: relative_path},
  enums: {field_name: [values]},
  terminal_states: [values],
  signal_prefixes: [prefix_strings]
}

Schema versions 8 and 9 introduced app-test fields (test_cadence,
test_exemption_reason, last_judge_model, last_judge_tokens). DEPRECATED
2026-06-08 — app-test surface removed per
docs/decision-app-tests-2026-06-08.md. The fields remain on the dataclass
for backward compatibility with on-disk manifests and will be dropped in
schema v11.

Schema version 10 adds the top-level `usage` block — the bot-facing operating
manual for the app, surfaced into INSTALLED_APPS.md and the AGENTS.md marker
section. Distinct from `description` (operator-facing what-it-is) and
`identity.purpose` (RSI-facing why-it-exists); `usage` answers *how the bot
should invoke this app* in conversation. Schema:
  {
    model: "user-initiated" | "scheduled" | "event-driven" | "ambient",
    trigger_recognition: {
      pattern: str,                 # one-sentence description of when to fire
      hint_words: list[str],        # surface forms that should make the bot reach for the app
      requires_keyword: bool,       # true = only fire on exact hint; false = topical match also OK
    },
    auto_capture: {
      enabled: bool,                # true = bot captures matching content without being told to
      sources: list[str],           # e.g. ["user message", "scheduled cron", "inbox webhook"]
    },
    how_to_use: str,                # paragraph: when to invoke, with what args, what to say back
    bot_voice_examples: list[str],  # short snippets of what the bot might say while using it
  }
All sub-fields are optional — generators read with `.get()` and fall back to
description / identity.purpose / capability_tags / session_keywords when usage
is missing.  Existing manifests get an empty dict via migrate_manifest and can
be enriched later via the spec wizard or a follow-up scanner pass.

Schema version 11 adds two app-audit fields surfaced by the per-bot audit
runner (see docs/spec-app-audit-2026-05-16.md):
  - last_structural_verify: dict — stamp written by audit_runner.py after
    each Tier-2 pass. Schema:
      {
        verified_at: str (ISO),
        runner_version: str,
        audit_run_id: str,
        status: "ok" | "ok_with_minor" | "warning" | "failed",
        findings_count: int,
        by_severity: {critical: int, major: int, minor: int, info: int},
      }
    Empty dict means "Tier 2 has never run for this app." Distinct from
    last_verification (which is the forge-time reality check).
  - audit_trail_path: str — absolute path on the bot to the per-app rolling
    audit log (`.../workspace/evolve/audits/<app_id>/trail.jsonl`). The
    admin UI deep-links here via the existing read-ACL on .openclaw/.

Schema version 13 adds scheduled-action contract fields surfaced by the
scanner's new extraction pass. See docs/spec-audit-extensions-2026-05-17.md
§3 — these close a class of silent-failure gap where heartbeat-embedded
recurring behaviors (e.g. the April-2026 protein-reminder regression) had
no manifest claim to verify against, so Tier-2 / Tier-3 audits had nothing
to catch when the heartbeat surface got clobbered.

  - scheduled_actions: list[dict] — recurring behaviors the bot is
    instructed to do periodically. One entry per behavior. Schema:
      [
        {
          "id": str,                       # stable per-app slug, e.g. "protein-6pm-tally"
          "trigger": {
            "kind": "heartbeat" | "cron" | "launchd" | "session_start",
            "schedule": str,               # human form, e.g. "18:00 daily"
            "evidence_path": str,          # relative to bot workspace
            "evidence_locator": str,       # heading text or unique phrase
            "section_sha256": str,         # sha at scan time, for drift detection
          },
          "inputs":  [{"path": str,
                       "kind": "data_file"|"external"|"dependency",
                       "from_dependency": str}],   # "dependency" → path is
                       # owned by a declared app_dependencies[] app
                       # (e.g. a watcher reading another app's output);
                       # from_dependency names that app's pkg_id/display_name.
                       # Coherence C-A2 resolves it against app_dependencies[].
          "outputs": [{"kind": str, "channel": str}],
          "summary": str,                  # one-line plain-language description
        }
      ]
    Empty list when the scanner has not yet run the extraction pass, or the
    app legitimately has no scheduled actions. Tier-2 assertions
    `scheduled_action_evidence_path` and `scheduled_action_anchor` verify
    these claims against on-disk reality.
  - heartbeat_evidence: dict — heartbeat surfaces the app's behaviors live
    in, beyond what scheduled_actions[] cites individually. Useful for
    "this app's behavior is documented in HEARTBEAT.md sections X and Y"
    claims that span multiple actions. Schema:
      {
        "file_path": str,
        "section_anchors": list[str],
      }
    Empty dict when not populated. Tier-2 assertion
    `heartbeat_anchors_present` verifies each anchor still resolves.
  - cron_evidence: dict — LaunchDaemon plist labels or crontab entries the
    app depends on, beyond what is owned by `crons[]`. Captures externally-
    installed crons (e.g., system-level launchd jobs, infrastructure crons
    that fire the bot's heartbeat-embedded behaviors). Schema:
      {
        "labels": list[str],
      }
    Empty dict when not populated. Tier-2 assertion `cron_labels_loaded`
    verifies each label appears in launchctl or crontab.

Schema version 12 adds the Tier-3 semantic audit fields. These extend the
audit surface from "wiring still holds" (Tier 2) to "code still matches the
manifest's claims" (Tier 3, LLM-driven). See docs/spec-app-audit-2026-05-16.md
§4 for the discovery + triage two-stage design, §5.5 for accepted-findings,
and §6 for the cadence model.

  - audit_cadence: str | None — per-app override of the pod-wide auto-audit
    cadence. None means "inherit pod default → per-bot default → built-in
    monthly." Valid values are in VALID_AUDIT_CADENCES below.
  - audit_eligible: bool (default true) — whether the per-bot auto-scheduler
    should consider this app. Forge sets this false for apps with no code
    worth auditing (pure-data manifests, static reference docs). Manual
    audits via CLI / UI / evo audit run regardless.
  - audit_accepted: list[dict] — operator-accepted finding signatures the
    triage stage drops on regular runs. The "Full audit" mode ignores this
    list and re-evaluates from scratch. Schema:
      [
        {
          "signature": str (sha256-shaped),
          "accepted_at": str (ISO),
          "accepted_by": str (user_key or "operator:ui"),
          "rationale": str,
        }
      ]
  - last_audit: dict — most recent Tier-3 run stamp. Empty when Tier-3 has
    never run for this app. Schema mirrors last_structural_verify shape but
    captures Tier-3 specific fields. Schema:
      {
        verified_at: str (ISO),
        runner_version: str,
        audit_run_id: str,
        status: "ok" | "with_findings" | "failed",
        findings_count: int,
        outcomes: {dismiss: int, auto_fix: int, propose: int, conflict_notice: int},
        tokens: int,
        full_audit: bool,
      }
"""

from __future__ import annotations

import copy as _copy
import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from evolve_util import now_iso  # re-exported; sibling modules import it from here

# Schema v25 app_kind enum default. Imported here (purpose_classifier is the
# single source of the kind vocabulary) — it is stdlib-only and never imports
# manifest, so there is no cycle.
from .purpose_classifier import APP_KIND_APPLICATION


def _new_file_id_import() -> str:
    """Lazy import wrapper to avoid circular import at module level."""
    from .ids import new_file_id
    return new_file_id()


# Field-vocabulary counter — bumped once per slice that adds dataclass fields.
# It tracks vocabulary, never shape (that's `manifest_shape` below); nothing
# branches on it at runtime. Every field block added to the dataclass must be
# labeled "Schema vN" with N <= this constant — enforced by
# tests/test_manifest_schema_version_guard.py per
# docs/spec-manifest-v7-slicing-2026-06-10.md §2.3.
# v23: delivery_contract{} on scheduled_actions[] entries
# (spec-proactive-delivery-monitor-2026-06-10.md §5).
# v24: privacy{} + audience_scoping{} top-level blocks — manifest-v7 Slice 2
# (spec-manifest-v7-slicing-2026-06-10.md §4, base spec §3 Atlas Gaps 3/4).
# v25: app_kind + classification{} — purpose/fit classifier (app-scan bite D1).
# v26: app_kind enum gains "system" (pod/agent-runtime infrastructure) +
# classifier_version stamped into classification{} — Scanner Slice 2. No new
# field block; the v25 fields' vocabulary widened (closed enum is now
# {application, capability, system}), so the counter advances with it.
# v27: definition_status (Defined/Discovered source-of-truth axis) — Bite 1 of
# the app-substrate lifecycle (docs/spec-apps-meta-2026-06-13.md §9). One new
# top-level field with inert default "discovered"; never branch on the counter.
# v28: drift_log (drift-narrative log) — Bite 3 of the same lifecycle
# (docs/spec-apps-meta-2026-06-13.md §9.3). One new top-level field with inert
# default []; never branch on the counter. NB the field is named drift_log,
# NOT change_log: the v7-arc Instance already carries a load-bearing,
# schema-locked change_log[] (forge/Lessons audit trail), so the drift narrative
# ships as a distinct field — the same collision resolution as v27's
# definition_status vs status (see the Schema v28 dataclass block + spec §9.7).
MANIFEST_SCHEMA_VERSION = 28

# v14 adds the `manifest_shape` discriminator field that distinguishes the legacy
# single-file shape from the v7-arc split (App Spec + Instance + Provenance +
# Lessons). Existing manifests carry an empty string (the legacy shape); the v7
# migration sets it to "v7-arc". See docs/spec-manifest-v7-2026-05-20.md.
MANIFEST_SHAPE_LEGACY = ""
MANIFEST_SHAPE_V7_ARC = "v7-arc"
# Gallery-install intermediate: a v7-arc Instance materialized from a gallery
# package before the scanner has reconciled its realized_files[]. The View
# endpoint and server.py special-case it (filename stem may diverge from id).
# It is a valid, transient shape — not corruption.
MANIFEST_SHAPE_V7_ARC_PRE = "v7-arc-pre"

# The `manifest_shape` discriminator is a closed enum. Anything outside this
# set — most often a schema-version string like "v20" that leaked into the
# field from an LLM-driven manifest synthesis or a hand edit — silently
# *orphans* the manifest: the legacy→v7-arc promotion path skips it (shape
# != "") AND every reader that branches on `== "v7-arc"` (reflect.py,
# pass_runner, audit, spec_drift, adopt, extend_application, …) skips it too.
# migrate_manifest() coerces any out-of-set value back to a canonical shape
# inferred from structure — see _normalize_manifest_shape().
VALID_MANIFEST_SHAPES = frozenset(
    {MANIFEST_SHAPE_LEGACY, MANIFEST_SHAPE_V7_ARC_PRE, MANIFEST_SHAPE_V7_ARC}
)

# ── Schema v15: per-app data classification for cloud backup ──────────────────
#
# Phase 3 of the backup architecture rework
# (spec-backup-and-data-classification-2026-05-28.md). Three optional fields:
#
#   app_files_privacy: "cloud" | "local"
#     Applies to the app's code + scaffolding (manifest itself, scripts,
#     anything in the existing `files:` list). Default "cloud".
#
#   data_paths: list[{path, privacy, note?}]
#     Per-directory rules for runtime data the app produces. Path is
#     relative to the bot workspace. privacy is one of "cloud" | "local"
#     | "ephemeral".
#
#   default_for_unclassified: "cloud" | "local" | "ephemeral"
#     Policy for files that don't match any declared data_paths entry.
#     Default "local" — safe-by-default direction (rather miss a backup
#     than leak sensitive data).
#
# All three fields are optional; their absence means the app inherits the
# pre-v15 behaviour of "everything is cloud-eligible." So existing v14
# manifests don't need to be touched for backups to keep working.

PRIVACY_CLOUD     = "cloud"
PRIVACY_LOCAL     = "local"
PRIVACY_EPHEMERAL = "ephemeral"
VALID_PRIVACY     = (PRIVACY_CLOUD, PRIVACY_LOCAL, PRIVACY_EPHEMERAL)
VALID_APP_FILES_PRIVACY = (PRIVACY_CLOUD, PRIVACY_LOCAL)


# ── Schema v16: scheduled_action install metadata ─────────────────────────────
#
# Phase 1 of the forge side-effects materialization
# (docs/spec-forge-side-effects-2026-06-02.md). Five new optional sub-fields
# within each ``scheduled_actions[]`` entry; the field itself was introduced
# in v13. The new sub-fields describe *how* an action is installed (the
# mechanism + declarative recipe) and *whether* it actually was (provenance
# stamps). Forge populates them at install time; the scanner backfills them
# for hand-installed crons/hooks during attribution (Part B of the spec).
#
#   mechanism: enum
#     One of SCHEDULED_ACTION_MECHANISMS. Discriminator for which install
#     surface owns this action. Missing/empty defaults to "unknown".
#
#   install: dict (declarative install recipe)
#     {command, cwd, output_signals[], silent_when_no_output, plist_label,
#      hook_event, exec_policy}.  Forge reads this to perform the install
#     in Phase 4.5 (the new "Materialize" step). Missing for actions whose
#     install predates v16 — the scanner is responsible for inferring and
#     backfilling them.
#
#   installed_at: ISO timestamp | None
#     When the action was last installed/updated. None means not yet
#     installed by forge.
#
#   installed_by: str | None
#     "forge:{job_id}" or "scanner:backfill" or operator name.
#
#   installed_artifact: str | None
#     Path-fragment back to the install site so verifier can find it:
#     "openclaw.json#hooks.heartbeat[2]", "/Users/{bot}/Library/
#     LaunchAgents/com.{bot}.task-check.plist", "crontab:line-3".
#
# None of these are required — pre-v16 entries continue to work and the
# scanner attribution pass populates them retroactively for existing apps.

# ── Schema v17: oc_heartbeat_instruction replaces oc_heartbeat_hook ──────────
#
# Spec: docs/spec-heartbeat-instruction-2026-06-03.md.
#
# The 2026-06-02 live validation of PR 4 (forge install phase) discovered
# that ``oc_heartbeat_hook``'s install path — patching ``openclaw.json``'s
# ``hooks.heartbeat[]`` array — is built on a wrong assumption. OpenClaw
# has no top-level ``hooks`` field; ``hooks.heartbeat[]`` doesn't exist
# in the OC config schema. ``safe_write_bot_config`` correctly rejected
# the patch ("hooks: Invalid input"), which is the defense-in-depth
# working as designed.
#
# How team-bot-a/c/d/security-bot actually implement heartbeat-driven
# behaviors: text in ``HEARTBEAT.md`` (e.g. "every heartbeat, run
# `python3 scripts/tasks.py check` and surface any TASK_DUE lines"),
# which the bot's session-driven LLM reads and executes when the
# heartbeat fires a turn.
#
# v17 introduces ``oc_heartbeat_instruction`` / ``oc_session_instruction``
# as the correct mechanism values and deprecates the bogus hook
# variants. The install recipe for the new mechanisms is:
#
#   {file: "HEARTBEAT.md", section_anchor: "## Section Name",
#    body: "Natural-language instruction", command: "actual cmd"}
#
# ``installed_artifact`` becomes ``HEARTBEAT.md#Section Name``.
#
# Migration: any pre-v17 entry carrying ``mechanism: oc_heartbeat_hook``
# (or _session_hook) is rewritten to the corresponding _instruction
# variant; ``installed_artifact`` is cleared so the next forge run
# re-installs via the correct mechanism. The deprecated values stay in
# ``_DEPRECATED_MECHANISMS`` for one version so the migration helper
# can spot them.

# Preferred values (use these for new entries).
MECHANISM_OC_HEARTBEAT_INSTRUCTION = "oc_heartbeat_instruction"
MECHANISM_OC_SESSION_INSTRUCTION   = "oc_session_instruction"
# v18 (2026-06-03) — Python-by-default scheduled action with Signal-store
# escalation. The intended default for any periodic check whose work
# can be done by a Python script and which only needs to wake the bot
# LLM when there's something actionable to surface. See
# docs/spec-launchd-python-signal-2026-06-03.md.
MECHANISM_LAUNCHD_PYTHON_SIGNAL    = "launchd_python_signal"
# Deprecated in v17 — see _migrate_mechanism_v17. Kept as constants so
# code that references them (gallery specs in flight, migration tests)
# continues to import cleanly.
MECHANISM_OC_HEARTBEAT_HOOK = "oc_heartbeat_hook"   # deprecated
MECHANISM_OC_SESSION_HOOK   = "oc_session_hook"     # deprecated
MECHANISM_LAUNCHD           = "launchd"
MECHANISM_CRONTAB           = "crontab"
MECHANISM_EXTERNAL          = "external"
MECHANISM_UNKNOWN           = "unknown"
SCHEDULED_ACTION_MECHANISMS = (
    MECHANISM_OC_HEARTBEAT_INSTRUCTION,
    MECHANISM_OC_SESSION_INSTRUCTION,
    MECHANISM_LAUNCHD_PYTHON_SIGNAL,
    MECHANISM_OC_HEARTBEAT_HOOK,
    MECHANISM_OC_SESSION_HOOK,
    MECHANISM_LAUNCHD,
    MECHANISM_CRONTAB,
    MECHANISM_EXTERNAL,
    MECHANISM_UNKNOWN,
)

# Mechanisms rejected for new entries from v17 onward. The migration
# helper rewrites these to their _instruction equivalents and clears
# ``installed_artifact`` so the next forge run re-installs via the
# correct surface.
_DEPRECATED_MECHANISMS: frozenset[str] = frozenset({
    MECHANISM_OC_HEARTBEAT_HOOK,
    MECHANISM_OC_SESSION_HOOK,
})

# v17 → preferred mapping. Used by ``_migrate_mechanism_v17``.
_DEPRECATED_MECHANISM_REWRITES: dict[str, str] = {
    MECHANISM_OC_HEARTBEAT_HOOK: MECHANISM_OC_HEARTBEAT_INSTRUCTION,
    MECHANISM_OC_SESSION_HOOK:   MECHANISM_OC_SESSION_INSTRUCTION,
}


# ── Schema v23: delivery_contract{} on scheduled_actions[] entries ───────────
#
# Spec: docs/spec-proactive-delivery-monitor-2026-06-10.md §5 (Option B
# layered over A). An OPTIONAL per-entry sub-block declaring when a
# scheduled action's user-facing delivery is due and what evidence proves
# it happened. The delivery_monitor daemon reads it; when absent, the
# monitor derives Option-A defaults (window = fire + 30 min; user-facing
# iff outputs[] declares a channel-kind output; evidence = per-mechanism
# defaults; heal = none — never force-run an app that hasn't asserted
# re-run safety).
#
#   delivery_contract: {
#     "user_facing": bool,        # this action's output reaches a person
#     "window_minutes": int,      # grace after the scheduled fire time
#     "evidence": {
#       "ran":       {"kind": "scheduler_state"},
#       "delivered": {"kind": "run_file", "path": "memory/<app>-runs/{date}.json"}
#                    # or {"kind": "signal_line", "pattern": "BRIEFING_SENT:",
#                    #     "log": "<workspace-relative log path>"}
#                    # or {"kind": "scheduler_state"} — for conditional-
#                    # delivery polling apps where "the poll ran" is the
#                    # strongest deterministic delivery proof available
#     },
#     "heal": "rerun" | "none"    # "rerun" is an assertion BY THE APP AUTHOR
#                                 # that a forced re-run is safe (the app
#                                 # enforces one-delivery-per-window itself,
#                                 # e.g. Morning Briefing v2's run-file
#                                 # idempotency). The monitor never infers it.
#   }
#
# All fields optional; absent fields fall back to the derived defaults
# above. Tier-2 validates the shape (``delivery_contract_invalid``) and
# that declared run_file evidence paths appear in
# ``interface_contract.data_files`` (``delivery_contract_evidence_undeclared``).
# ``{date}`` in a run_file path is substituted with the delivery window's
# YYYY-MM-DD date by the monitor; gallery data_files declarations spell it
# ``YYYY-MM-DD`` — the Tier-2 check normalizes both forms.

DELIVERY_EVIDENCE_SCHEDULER_STATE = "scheduler_state"
DELIVERY_EVIDENCE_RUN_FILE        = "run_file"
DELIVERY_EVIDENCE_SIGNAL_LINE     = "signal_line"
DELIVERY_EVIDENCE_KINDS = (
    DELIVERY_EVIDENCE_SCHEDULER_STATE,
    DELIVERY_EVIDENCE_RUN_FILE,
    DELIVERY_EVIDENCE_SIGNAL_LINE,
)
DELIVERY_HEAL_RERUN = "rerun"
DELIVERY_HEAL_NONE  = "none"
DELIVERY_HEAL_VALUES = (DELIVERY_HEAL_RERUN, DELIVERY_HEAL_NONE)
_DELIVERY_CONTRACT_KEYS = frozenset(
    {"user_facing", "window_minutes", "evidence", "heal"}
)
_DELIVERY_EVIDENCE_ROLES = frozenset({"ran", "delivered"})


def validate_delivery_contract(contract: Any) -> list[str]:
    """Return a list of shape errors for one ``delivery_contract`` block.

    Empty list means valid. Pure (no I/O) — shared by the Tier-2
    structural assertion and the delivery_monitor daemon, so both agree
    on what a well-formed contract is. A malformed contract is reported
    by Tier-2; the monitor falls back to the derived Option-A defaults
    rather than guessing at the author's intent.
    """
    errors: list[str] = []
    if not isinstance(contract, dict):
        return [f"delivery_contract must be a dict, got {type(contract).__name__}"]
    for key in sorted(set(contract) - _DELIVERY_CONTRACT_KEYS):
        errors.append(f"unknown key {key!r}")
    if "user_facing" in contract and not isinstance(contract["user_facing"], bool):
        errors.append("user_facing must be a bool")
    if "window_minutes" in contract:
        wm = contract["window_minutes"]
        if not isinstance(wm, int) or isinstance(wm, bool) or wm < 1:
            errors.append("window_minutes must be an int >= 1")
    if "heal" in contract and contract["heal"] not in DELIVERY_HEAL_VALUES:
        errors.append(
            f"heal must be one of {DELIVERY_HEAL_VALUES}, got {contract['heal']!r}"
        )
    if "evidence" in contract:
        evidence = contract["evidence"]
        if not isinstance(evidence, dict):
            errors.append("evidence must be a dict")
        else:
            for role in sorted(set(evidence) - _DELIVERY_EVIDENCE_ROLES):
                errors.append(f"evidence: unknown role {role!r}")
            for role in sorted(_DELIVERY_EVIDENCE_ROLES & set(evidence)):
                entry = evidence[role]
                if not isinstance(entry, dict):
                    errors.append(f"evidence.{role} must be a dict")
                    continue
                kind = entry.get("kind")
                if kind not in DELIVERY_EVIDENCE_KINDS:
                    errors.append(
                        f"evidence.{role}.kind must be one of "
                        f"{DELIVERY_EVIDENCE_KINDS}, got {kind!r}"
                    )
                    continue
                if kind == DELIVERY_EVIDENCE_RUN_FILE:
                    path = entry.get("path")
                    if not isinstance(path, str) or not path.strip():
                        errors.append(f"evidence.{role}: run_file requires a path")
                    elif path.startswith("/") or ".." in path.split("/"):
                        errors.append(
                            f"evidence.{role}.path must be workspace-relative "
                            f"with no '..' segments, got {path!r}"
                        )
                if kind == DELIVERY_EVIDENCE_SIGNAL_LINE:
                    pattern = entry.get("pattern")
                    if not isinstance(pattern, str) or not pattern.strip():
                        errors.append(
                            f"evidence.{role}: signal_line requires a pattern"
                        )
                    log = entry.get("log")
                    if log is not None and (
                        not isinstance(log, str) or not log.strip()
                    ):
                        errors.append(
                            f"evidence.{role}.log must be a non-empty string "
                            f"when present"
                        )
    return errors


# Test cadence constants + VALID_TEST_CADENCES / DEFAULT_TEST_CADENCE
# removed 2026-06-08 — app-test surface killed per
# docs/decision-app-tests-2026-06-08.md. Manifest's `test_cadence` field is
# preserved on the dataclass for backward compatibility but no longer
# resolved or enforced.


# ── Audit cadence values ──────────────────────────────────────────────────────
#
# Distinct from test cadence — these gate the per-bot audit_runner's Tier-3
# (semantic) audit. Tier-2 (structural) runs unconditionally every 6 hours
# and isn't governed by these values.
#
#   never      — auto-audit disabled; only manual / on-demand runs ever execute.
#   quarterly  — auto-audit every 90 days.
#   monthly    — auto-audit every 30 days.  (Default.)
#   weekly     — auto-audit every 7 days.
#   daily      — auto-audit every 24 hours.  Use sparingly — costs scale linearly.
#
# audit_cadence on a manifest may also be None, meaning "inherit from pod /
# bot config." See docs/spec-app-audit-2026-05-16.md §6 for the three-layer
# resolution model (pod default → per-bot override → per-app override).

AUDIT_CADENCE_NEVER     = "never"
AUDIT_CADENCE_QUARTERLY = "quarterly"
AUDIT_CADENCE_MONTHLY   = "monthly"
AUDIT_CADENCE_WEEKLY    = "weekly"
AUDIT_CADENCE_DAILY     = "daily"

VALID_AUDIT_CADENCES: tuple[str, ...] = (
    AUDIT_CADENCE_NEVER,
    AUDIT_CADENCE_QUARTERLY,
    AUDIT_CADENCE_MONTHLY,
    AUDIT_CADENCE_WEEKLY,
    AUDIT_CADENCE_DAILY,
)
DEFAULT_AUDIT_CADENCE = AUDIT_CADENCE_MONTHLY

# Cadence → minimum interval (days) between auto-audits. The audit_runner's
# hourly tick consults `manifest.last_audit.verified_at` and skips apps that
# fired more recently than this. Full audits (operator-triggered with the
# --ignore-accepted flag) fire at 2× the configured cadence so the
# accepted-findings list gets periodic re-checks (spec §5.5).
AUDIT_CADENCE_INTERVAL_DAYS: dict[str, int] = {
    AUDIT_CADENCE_DAILY:     1,
    AUDIT_CADENCE_WEEKLY:    7,
    AUDIT_CADENCE_MONTHLY:   30,
    AUDIT_CADENCE_QUARTERLY: 90,
}


def effective_audit_cadence(
    manifest_audit_cadence: str | None,
    bot_id: str,
    network: dict,
) -> str:
    """Resolve effective audit cadence: per-app → per-bot → pod default.

    Mirrors the spec-app-audit-2026-05-16.md §6 three-layer model:
      1. Per-app override (`manifest.audit_cadence`)
      2. Per-bot override (`network.app_audit.bot_cadence.<bot_id>`)
      3. Pod default (`network.app_audit.default_cadence`)
      4. Built-in DEFAULT_AUDIT_CADENCE

    Invalid values at any layer fall through to the next layer rather than
    crashing the runner — the resolver should never refuse to return a value.
    """
    if manifest_audit_cadence in VALID_AUDIT_CADENCES:
        return manifest_audit_cadence
    app_audit = (network or {}).get("app_audit") or {}
    per_bot = (app_audit.get("bot_cadence") or {}).get(bot_id)
    if per_bot in VALID_AUDIT_CADENCES:
        return per_bot
    pod_default = app_audit.get("default_cadence")
    if pod_default in VALID_AUDIT_CADENCES:
        return pod_default
    return DEFAULT_AUDIT_CADENCE


# Forge-time test gate (ForgeTestGateError + validate_test_gate) was
# removed 2026-06-08 — app-test surface killed per
# docs/decision-app-tests-2026-06-08.md. Coherence gate
# (validate_coherence_gate) remains as the load-bearing forge-approval
# barrier.


# ── Pre-deploy coherence gate (spec §6.6) ─────────────────────────────────
#
# Pass A is a BLOCKER for forge approval + manual manifest editor saves;
# incoherence requires operator override. ``ForgeCoherenceGateError``
# raises from ``validate_coherence_gate`` when Pass A status is
# incoherent and no matching override_key was provided.

class ForgeCoherenceGateError(ValueError):
    """Raised when a manifest reaches forge approval with incoherent Pass A.

    Carries the ``override_key`` so the admin UI / forge bot can offer the
    operator an override path (spec §6.6).
    """
    def __init__(self, message: str, override_key: str = "",
                 findings: list[dict] | None = None) -> None:
        super().__init__(message)
        self.override_key = override_key
        self.findings = findings or []


def validate_coherence_gate(
    manifest_dict: dict,
    *,
    bot_id: str = "",
    app_id: str = "",
    override_key: str | None = None,
) -> None:
    """Pre-deploy coherence gate for forge approval (spec §6.6).

    Calls ``pre_deploy_gate.forge_approval_gate`` against a copy of the
    manifest. Raises ``ForgeCoherenceGateError`` when the verdict is
    not allowed (incoherent without matching override_key).

    Accepts a dict (not ApplicationManifest) so the gate runs against
    the same shape Pass A operates on without dataclass roundtrips.

    Soft-fails (returns silently) when ``pre_deploy_gate`` can't be
    loaded — the forge has additional gates downstream, so an import
    failure here doesn't mean nothing checks coherence.
    """
    try:
        from .pre_deploy_gate import forge_approval_gate
    except Exception:
        return
    verdict = forge_approval_gate(manifest_dict, override_key=override_key)
    if verdict.allowed:
        return
    # Build a helpful message that includes the override_key so the
    # operator can submit it back if they choose to ship anyway.
    finding_lines = []
    for f in verdict.findings[:5]:
        sev = (f.get("severity") or "").upper()
        desc = f.get("description") or f.get("message") or "?"
        finding_lines.append(f"  - {sev} {f.get('id') or f.get('check_id') or ''}: {desc[:120]}")
    extra = f" (+{len(verdict.findings) - 5} more)" if len(verdict.findings) > 5 else ""
    where = f"{bot_id}/{app_id}" if bot_id and app_id else app_id or "manifest"
    raise ForgeCoherenceGateError(
        f"App {where} blocked by coherence gate (status: {verdict.status}):\n"
        + "\n".join(finding_lines) + extra
        + f"\nOverride this gate by re-submitting with override_key={verdict.override_key!r}.",
        override_key=verdict.override_key,
        findings=verdict.findings,
    )


# ── Manifest source values ─────────────────────────────────────────────────────
#
# Records how/why a manifest came into existence.  Set at creation, never changed.

MANIFEST_SOURCE_DISCOVERED     = "discovered"       # auto-scanner found it in a workspace
MANIFEST_SOURCE_USER_CREATED   = "user_created"     # operator built it manually in the admin UI
MANIFEST_SOURCE_BOT_CREATED    = "bot_created"      # bot was instructed to build it (prompt/instructions)
MANIFEST_SOURCE_RSI_PROPOSED   = "rsi_proposed"     # RSI engine proposed and auto-generated it
MANIFEST_SOURCE_FILE_IMPORTED  = "file_imported"    # operator uploaded a manifest JSON directly
MANIFEST_SOURCE_GALLERY        = "gallery_installed"  # installed from the app gallery


# ── Per-file provenance (smart forge, docs/note-smart-forge-and-file-
# provenance-2026-06-04.md). Each entry in ``ApplicationManifest.files[]``
# may carry a ``provenance`` key whose value is one of these constants.
# When absent, ``ApplicationManifest.files_partition()`` infers a value
# from whether the manifest has a stamped ``files_pack`` block (preserves
# pre-existing behaviour exactly — see the note's Compatibility section).
FILE_PROVENANCE_BUNDLED        = "bundled"     # comes from gallery/<slug>/files/
FILE_PROVENANCE_FORGE          = "forge"       # LLM-generated at install time

# Backward-compat aliases (older scanner used these)
_LEGACY_SOURCE_MAP = {
    "detected":    MANIFEST_SOURCE_DISCOVERED,
    "user-defined": MANIFEST_SOURCE_USER_CREATED,
    "imported":    MANIFEST_SOURCE_FILE_IMPORTED,
    "llm-inferred": MANIFEST_SOURCE_DISCOVERED,
}


# ── Definition status (source-of-truth axis) ────────────────────────────────────
#
# Schema v27. The Defined/Discovered manifest lifecycle (Bite 1 of the
# app-substrate lifecycle work; docs/spec-apps-meta-2026-06-13.md §9).
#
# This is a NEW, third axis on every manifest, deliberately distinct from the
# two pre-existing ones it is easy to confuse with:
#
#   ``status``  (lifecycle)  — active | paused | draft | deprecated | hidden |
#                              dormant. "Is this app on / off / retired."
#   ``source``  (creation)   — discovered | user_created | gallery_installed |
#                              … (see MANIFEST_SOURCE_*). Set once at creation,
#                              never changed. "How did this manifest first come
#                              to exist."
#   ``definition_status``    — discovered | defined. The SOURCE-OF-TRUTH axis.
#                              "Has an operator vouched for this manifest as the
#                              authoritative contract, or is it still a
#                              scanner-owned churnable draft."
#
# Why a new field and not ``status``: ``status`` already carries the lifecycle
# vocabulary above and dozens of consumers branch on it. Overloading it would
# be a silent semantic collision. The spec §9.1 names a "status" axis of
# discovered ↔ defined; the implemented field is named ``definition_status`` to
# avoid that collision (resolution documented in spec §9.7).
#
#   discovered — scanner-owned draft. The scanner may freely re-mint / merge /
#                rename / archive it on any pass. This is the root-cause fix for
#                the scanner-FP saga: the scanner is author AND editor of every
#                manifest with no authoritative anchor, so every scan can churn
#                it. ``discovered`` makes that churnability explicit.
#   defined    — operator-vouched source of truth. Promotion marks the
#                anchored-identity fields (name + canonical identity line) as
#                authored (provenance.field_origins) so reconciliation surfaces
#                drift as a chip instead of silently overwriting, and the L3
#                archival shield never auto-archives it (existence guarantee),
#                even with zero files on disk.
#
# Reversible: promote (discovered → defined) and demote (defined → discovered)
# are both operator actions; promotion is NOT permanent. Readers MUST branch on
# this field, never on MANIFEST_SCHEMA_VERSION. Absent/empty reads as
# ``discovered`` (the safe default — never accidentally vouched or shielded).
MANIFEST_DEFINITION_DISCOVERED = "discovered"
MANIFEST_DEFINITION_DEFINED    = "defined"
VALID_MANIFEST_DEFINITION_STATUSES = frozenset({
    MANIFEST_DEFINITION_DISCOVERED,
    MANIFEST_DEFINITION_DEFINED,
})


def born_definition_status(source: str) -> str:
    """Derive the born ``definition_status`` for a NEWLY-created manifest from
    its creation ``source``.

    Mapping (the same partition as the scanner's operator-authored shield):
      * ``discovered`` / empty / legacy-scanner alias  → ``discovered``
      * every authored source (user_created, gallery_installed, bot_created,
        rsi_proposed, file_imported, forge_built, …)   → ``defined``

    Rationale: an operator/forge/gallery/RSI/bot install is an explicit act of
    creation — the operator already vouched by installing it, so a fresh
    authored manifest is born ``defined``. A scanner discovery is an inference
    with no human vouch, so it is born ``discovered`` (churnable until promoted).

    NOTE — this is a CREATE-TIME helper only. It is intentionally NOT applied
    to existing on-disk manifests: migration lands ALL pre-existing manifests
    at ``discovered`` regardless of source (no bulk auto-promote — see
    migrate_manifest's defaults). Calling this on a re-save would silently
    auto-promote, so call it once, at the creation site, before first write.
    """
    s = (source or "").strip().lower()
    s = _LEGACY_SOURCE_MAP.get(s, s)
    if not s or s == MANIFEST_SOURCE_DISCOVERED:
        return MANIFEST_DEFINITION_DISCOVERED
    return MANIFEST_DEFINITION_DEFINED


# ── Cron helpers ──────────────────────────────────────────────────────────────

def _parse_cron_string(s: str) -> dict:
    """
    Parse a raw crontab line into a cron dict.

    Handles both standard 5-field cron schedules and @reboot / @hourly etc.
    Splits off the script path from the command portion.

    Examples:
        "0 2 * * * /path/to/script.py"  → {schedule:"0 2 * * *", script:"/path/to/script.py", ...}
        "@reboot python3 /path/to/x.py" → {schedule:"@reboot",   script:"/path/to/x.py", ...}
    """
    s = s.strip()
    if not s:
        return {"schedule": "", "script": s, "label": "", "file_id": ""}

    # Handle @keyword crons
    if s.startswith("@"):
        parts = s.split(None, 1)
        schedule = parts[0]
        cmd = parts[1] if len(parts) > 1 else ""
    else:
        parts = s.split()
        if len(parts) < 6:
            # Malformed — treat the whole string as schedule, empty script
            return {"schedule": s, "script": "", "label": "", "file_id": ""}
        schedule = " ".join(parts[:5])
        cmd = " ".join(parts[5:])

    # Extract the script path: last token ending in .py/.sh, else the full cmd
    cmd_parts = cmd.split()
    script = next(
        (p for p in reversed(cmd_parts) if p.endswith((".py", ".sh"))),
        cmd_parts[0] if cmd_parts else cmd,
    )
    return {"schedule": schedule, "script": script, "label": "", "file_id": ""}


def _promote_cron(entry: "str | dict") -> dict:
    """
    Normalise a cron entry (string or partial dict) to a full cron dict.

    Always returns a dict with keys: schedule, script, label, file_id.
    Preserves an existing file_id; never overwrites it with an empty value.
    """
    if isinstance(entry, str):
        return _parse_cron_string(entry)
    if isinstance(entry, dict):
        return {
            "schedule": entry.get("schedule", ""),
            "script":   entry.get("script", entry.get("script_path", "")),
            "label":    entry.get("label", entry.get("description", "")),
            "file_id":  entry.get("file_id", ""),
        }
    return {"schedule": str(entry), "script": "", "label": "", "file_id": ""}


def _cron_line(entry: "str | dict") -> str:
    """
    Return the raw crontab-style line for a cron entry.

    Used to reconstruct a comparison key for matching against live crontab output.
    """
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, dict):
        s = entry.get("schedule", "").strip()
        sc = entry.get("script", entry.get("script_path", "")).strip()
        return f"{s} {sc}".strip() if sc else s
    return str(entry)


@dataclass
class ApplicationTest:
    """Legacy test case dataclass — kept for backward compat with reviewer."""
    name: str
    description: str
    trigger: str           # What causes this to run
    expect: str            # What passing looks like
    test_type: str = "behavioral"
    script: str = ""
    privacy: bool = False


@dataclass
class SuccessMetric:
    """Legacy success metric dataclass — kept for backward compat."""
    name: str
    description: str
    measurement: str
    target: str
    metric_type: str = "qualitative"


@dataclass
class ApplicationManifest:
    """Full manifest for one capability."""
    id: str                          # slug, e.g. "health-tracking"
    name: str                        # Human name, e.g. "Health Tracking"
    bot_id: str

    # Core descriptive
    description: str = ""
    purpose: str = ""                # 2-3 sentence why-this-matters

    # Status and provenance
    status: str = "active"           # active | paused | draft | deprecated | hidden | dormant
    source: str = MANIFEST_SOURCE_DISCOVERED  # see MANIFEST_SOURCE_* constants
    source_detail: str = ""          # free-form: scan run id, forge job id, UI session, etc.
    confidence: float = 0.0

    # Goals and behaviour
    goals: list[str] = field(default_factory=list)
    example_triggers: list[str] = field(default_factory=list)

    # DEPRECATED 2026-06-08: app-test surface removed; see
    # docs/decision-app-tests-2026-06-08.md. Field kept so existing on-disk
    # manifests load without error. Will be dropped in schema v11.
    test_cases: list[dict] = field(default_factory=list)

    # Privacy
    privacy_constraints: list[str] = field(default_factory=list)

    # Evidence
    evidence_files: list[str] = field(default_factory=list)

    # Satisfaction
    satisfaction_score: int | None = None
    satisfaction_notes: str | None = None

    # DEPRECATED 2026-06-08: app-test surface removed; see decision memo.
    # Fields kept for backward compatibility; will be dropped in schema v11.
    last_tested: str | None = None
    last_test_result: str | None = None   # pass | fail | partial

    # History
    improvement_history: list[dict] = field(default_factory=list)
    known_issues: list[str] = field(default_factory=list)

    # New 4-section RSI fields (schema v3)
    # identity: {purpose, scope_includes, scope_excludes, user}
    identity: dict = field(default_factory=dict)
    # success_criteria: {observable_outcomes, failure_signals, quality_bar}
    success_criteria: dict = field(default_factory=dict)
    # constraints: {privacy, safety, dependencies, boundaries}
    constraints: dict = field(default_factory=dict)
    # satisfaction: {score, notes, rated_at}
    satisfaction: dict = field(default_factory=dict)
    # open_questions: list of strings
    open_questions: list = field(default_factory=list)

    # Tags
    tags: list[str] = field(default_factory=list)

    # Timestamps
    created_at: str = ""
    updated_at: str = ""

    # ── Schema v4: operational/registry fields ────────────────────────────────
    # Semver of the app itself (distinct from schema_version)
    app_version: str = ""
    # One-sentence goal statement (complements v3 `purpose`)
    objective: str = ""
    # Bot account that owns/runs this app
    owner: str = ""
    # Humans responsible for this app
    maintainers: list[str] = field(default_factory=list)
    # All scripts, code files, and assets (relative to workspace root)
    files: list[str] = field(default_factory=list)
    # Cron entries belonging to this app.
    # v4: list[str] raw crontab lines ("0 2 * * * /path/script.py")
    # v5+: list[dict] with keys: schedule, script, label, file_id
    # Use manifest.cron_dicts() for backward-compatible dict access.
    crons: list = field(default_factory=list)
    # What the app consumes
    inputs: list[str] = field(default_factory=list)
    # What the app produces
    outputs: list[str] = field(default_factory=list)
    # Named functions/hooks other apps or bots can call
    exported_hooks: list[str] = field(default_factory=list)
    # DEPRECATED 2026-06-08: app-test surface removed; see decision memo.
    # Field kept for backward compatibility; will be dropped in schema v11.
    test_command: str = ""
    # Named operational metrics tracked during normal operation (labels for RSI loop)
    rsigrade_signals: list[str] = field(default_factory=list)
    # Paths to documentation files
    docs: list[str] = field(default_factory=list)
    # ISO date of last human review
    last_reviewed: str = ""
    # DEPRECATED 2026-06-08: smoke-run trail. Will be dropped in schema v11.
    last_test_run: str = ""
    last_test_output: str = ""
    last_test_exit_code: int | None = None
    # Last post-apply verification result (Phase C). Written by
    # bot_forge.verify_manifest_reality after _apply_forge_output saves the
    # manifest. Schema:
    #   {
    #     "verified_at": ISO,
    #     "status": "ok" | "warning" | "failed",
    #     "summary": str,
    #     "files": {"ok": list[path], "missing": [...], "sha_mismatch": [...]},
    #     "errors": list[str],
    #   }
    # Empty dict means verification has never run for this manifest.
    last_verification: dict = field(default_factory=dict)
    # If True, this app is intentionally unregistered and should not be flagged
    compliance_suppressed: bool = False
    compliance_suppressed_reason: str = ""

    # ── Schema v5: gallery & forge provenance fields ──────────────────────────
    # Stable package identity assigned at gallery creation — never changes
    pkg_id: str = ""                     # e.g. "p-a3f91c8b"
    # Current local version (CalVer + major.minor), increments each forge run
    pkg_version: str = ""                # e.g. "2026.04.15-1.3"
    # Gallery blueprint version at install time — frozen until operator pulls update
    gallery_version: str = ""            # e.g. "2026.04.10-1.0"
    # Human-readable display name (separate from slug `id`)
    display_name: str = ""               # e.g. "Task Manager"
    # Package author identifier
    author: str = ""                     # e.g. "openclaw-gallery"
    # Markdown build specification — primary input to forge run #0 (install)
    build_spec: str = ""
    # Active forge job reference (null when no job is running)
    # Schema: {job_id, run_id, current_step, steps_total, phase, last_updated}
    install_job: dict | None = None
    # Files this app uses but does not own (owned by another pkg_id)
    # Schema: [{file_id, path, owned_by, purpose}]
    dependencies: list[dict] = field(default_factory=list)
    # NOTE: `files` field (inherited from v4 as list[str]) is promoted to list[dict] in v5.
    # Each dict: {file_id, file_version, path, purpose, owned_by, shared_with,
    #             created_in_run, last_modified_in_run, created_at, modified_at}
    # Use manifest.file_paths() for backward-compatible path access regardless of format.

    # ── Schema v6: dependency resolution & interface contract fields ──────────
    # App-level package dependencies — other gallery apps that must be installed
    # on this bot before this app can be forge-built.  Distinct from `dependencies`
    # above, which tracks cross-app file sharing at the file level.
    # Schema: [{pkg_id, display_name, required: bool, reason}]
    app_dependencies: list[dict] = field(default_factory=list)

    # Technical prerequisites: integrations, secrets, system tools, python packages.
    # Checked by the preflight system before a forge job is queued.
    # Schema: {integrations: [...], secrets: [...], system: [...], python_packages: [...]}
    # See module docstring for full sub-schemas.
    requirements: dict = field(default_factory=dict)

    # Stable external interface surfaces.  Normative in gallery manifests; authoritative
    # (overwritten by forge) in installed manifests after each build.  Injected as
    # context when forge builds an app that lists this app as an app_dependency.
    # Schema: {populated_by_forge, extracted_at, data_files, cli, key_paths,
    #          enums, terminal_states, signal_prefixes}
    # See module docstring for full sub-schema.
    interface_contract: dict = field(default_factory=dict)

    # ── Schema v7: session attribution hints ─────────────────────────────────
    # Short labels matched by the app-session correlator's Tier 1 (capabilities
    # name matching).  Auto-populated from display_name / name tokens; operators
    # can add synonyms (e.g. "links", "bookmarks") to improve attribution recall.
    capability_tags: list[str] = field(default_factory=list)
    # Phrases matched against session outcome text (correlator Tier 3 fallback).
    # Auto-populated from display_name tokens; extend for domain-specific terms.
    session_keywords: list[str] = field(default_factory=list)

    # ── Schema v8: app testing cadence & exemption (DEPRECATED 2026-06-08)
    # App-test surface removed; see docs/decision-app-tests-2026-06-08.md.
    # Fields kept for backward compatibility; will be dropped in schema v11.
    test_cadence: str | None = None
    test_exemption_reason: str = ""

    # ── Schema v10: bot-facing usage manual ──────────────────────────────────
    # The "how the bot should invoke this app" block.  Surfaced into
    # INSTALLED_APPS.md and the AGENTS.md marker section so the bot's LLM has
    # explicit guidance — distinct from `description` (operator-facing) and
    # `identity.purpose` (RSI-facing).  See module docstring for sub-schema.
    # Optional throughout; app_registry.py reads with fallback chain.
    usage: dict = field(default_factory=dict)

    # ── Schema v11: app-audit telemetry (Tier 2) ─────────────────────────────
    # Stamp from the per-bot audit_runner after each Tier-2 pass. Empty dict
    # means audits have never run for this app. See module docstring.
    last_structural_verify: dict = field(default_factory=dict)
    # Absolute path on the bot to the per-app trail.jsonl. Admin UI reads via
    # the existing ACL on .openclaw/. Populated lazily on first audit run.
    audit_trail_path: str = ""

    # ── Schema v12: Tier-3 semantic audit fields ─────────────────────────────
    # Per-app override of the pod-wide auto-audit cadence. None means "inherit
    # pod / bot config." One of VALID_AUDIT_CADENCES otherwise.
    audit_cadence: str | None = None
    # When False, the bot's audit_runner skips this app on auto-audit ticks.
    # Manual runs (CLI, UI, evo audit) execute regardless. Forge sets this
    # based on the app's shape during build; operators can override.
    audit_eligible: bool = True
    # Operator-accepted finding signatures the triage stage drops on normal
    # runs. The "Full audit" mode (--ignore-accepted) bypasses this. See
    # module docstring for entry schema.
    audit_accepted: list[dict] = field(default_factory=list)
    # Stamp from the most recent Tier-3 run. Empty dict means Tier 3 has
    # never run for this app. See module docstring for entry schema.
    last_audit: dict = field(default_factory=dict)

    # ── Schema v13: scheduled-action contracts ───────────────────────────────
    # Recurring behaviors extracted by the scanner from heartbeat / cron /
    # standing-instruction surfaces. Tier-2 verifies the evidence anchor
    # still resolves and the cited section hasn't drifted substantially.
    # See module docstring for entry schema.
    scheduled_actions: list[dict] = field(default_factory=list)
    # Heartbeat surfaces the app's behaviors live in. Tier-2 verifies each
    # section_anchor is still present in the cited file.
    heartbeat_evidence: dict = field(default_factory=dict)
    # External crons (launchd labels, crontab entries) the app depends on
    # beyond what's listed in `crons[]`. Tier-2 verifies each label is
    # loaded.
    cron_evidence: dict = field(default_factory=dict)

    # ── Schema v15: per-app data classification for cloud backup ─────────────
    # Phase 3 of spec-backup-and-data-classification-2026-05-28.md.
    #
    # All three fields are *optional* — absence means "no classification
    # declared, treat everything as cloud-eligible" (pre-v15 behaviour).
    # The classification resolver in analyzer.data_classification reads
    # these.
    #
    # app_files_privacy controls the app's code/scaffolding (the existing
    # `files:` list); data_paths declares per-directory rules for runtime
    # data; default_for_unclassified is the catch-all for new files that
    # don't match any declared rule. Empty string / empty list / None means
    # "not declared" so app_files_privacy can keep a meaningful empty
    # default without disabling backups by surprise.
    app_files_privacy: str = ""          # "" | "cloud" | "local"
    data_paths: list[dict] = field(default_factory=list)
    default_for_unclassified: str = ""   # "" | "cloud" | "local" | "ephemeral"

    # ── Schema v19: files-pack metadata for the hybrid install path ──────────
    # Spec: docs/spec-files-pack-hybrid-2026-06-03.md.
    #
    # When ``files_pack`` is non-empty AND ``gallery/<slug>/files/`` exists,
    # forge install takes the FAST PATH (copy + variable substitution,
    # ~$0 cost) instead of the LLM-driven build/critique/refine path. The
    # build_spec stays first-class — it's the durable contract that the
    # files-pack is a snapshot of. forge --regenerate forces the LLM
    # path and optionally updates the files-pack at the end.
    #
    # Shape (None or empty dict when no files-pack):
    #   {
    #     "format_version": "1.0",                  # this spec; bump if
    #                                               # the per-file metadata
    #                                               # shape evolves
    #     "files_count": 6,                          # number of files in
    #                                               # the pack
    #     "snapshot_source_pkg_version": "...",     # which pkg_version
    #                                               # produced this pack
    #     "sha256": "<sha256 of files/manifest.json>"
    #                                               # stale-detection
    #   }
    #
    # Per-file metadata (path, mode, sha256, placeholders) lives in
    # ``gallery/<slug>/files/manifest.json`` — too large for the package
    # manifest; the package only carries enough to detect drift.
    files_pack: dict = field(default_factory=dict)

    # ── Schema v20: coherence + reconciliation + provenance ──────────────────
    # Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §3, §4,
    # §5, §6. Adding these to the dataclass (rather than leaving them as
    # migration-only top-level keys) is what lets ``from_dict`` round-trip
    # the values — otherwise every save loses the in-memory provenance state
    # since ``to_dict() = asdict(self)`` only emits declared fields, and
    # ``from_dict`` filters out unknown keys at line 1029.
    #
    #   provenance     — manifest_origin + field_origins; gates whether
    #                    reconciliation chips fire (observational fields
    #                    update silently; authored fields stage). PR 2 wires
    #                    every write path to stamp the right source here.
    #
    #   reconciliation — staging area for chip-bound deltas. Empty/ok on a
    #                    healthy manifest; PR 4 (scanner reconciliation
    #                    pass) populates extra_files / missing_files / etc.
    #
    #   coherence      — claim-vs-mechanism findings + accepted signatures.
    #                    Empty/ok on a healthy manifest; PR 5 + PR 12–14
    #                    populate via Pass A/B/C1/C2/C3.
    #
    #   volatile_paths — declared-volatile directory globs (data/, log/,
    #                    content/, etc.). When populated, reconciliation
    #                    skips per-file enumeration inside the glob.
    #
    # Default values mirror what _populate_provenance_v20 / the defaults
    # dict in migrate_manifest produce on a fresh migrate. ``from_dict``
    # then round-trips them faithfully.
    provenance: dict = field(default_factory=dict)
    reconciliation: dict = field(default_factory=lambda: {
        "last_reconciled_at": None,
        "status": "ok",
        "extra_files": [],
        "missing_files": [],
        "missing_crons": [],
        "missing_actions": [],
        "volatile_growth_anomalies": [],
        "operator_decisions": [],
    })
    coherence: dict = field(default_factory=lambda: {
        "last_checked_at": None,
        "status": "ok",
        "findings": [],
        "last_capability_check": None,
        "coherence_accepted": [],
    })
    volatile_paths: list = field(default_factory=list)

    # ── Schema v21: agent-freelance-bypass Phase 2 (Layer A) ─────────────────
    # Spec: docs/spec-agent-freelance-bypass-phase2-2026-06-06.md.
    #
    # These three fields drive the install-time validator
    # (bot_guidance_freelance_validator), the audit Signal producer
    # (agent_bypass_audit), and the plugin's before_prompt_build
    # interceptor (Layer C). Without them on the dataclass, from_dict
    # silently drops the values on every manifest load — and the
    # validator's getattr-based read returns None / empty, so every
    # manifest would pass the gate (false negative).
    #
    #   bot_guidance    — list[dict] of {section, content} blocks spliced
    #                     into AGENTS.md. Currently the only place the
    #                     "do not freelance" instruction lives; Phase 2's
    #                     validator scans it for at-risk-shaped markers.
    #
    #   event_triggers  — list[dict] of structured chat-message → handler
    #                     routes. Phase 2.1 locked the match{} shape and
    #                     added the invocation{} sub-object; see the v7
    #                     spec schema for full shape.
    #
    #   invocation_mode — string enum: 'agent_invokes' (default —
    #                     LLM-driven via bot_guidance prose),
    #                     'plugin_intercept' (Layer C structural
    #                     enforcement), or 'subagent' (reserved for
    #                     deferred Layer B).
    bot_guidance: list = field(default_factory=list)
    event_triggers: list = field(default_factory=list)
    invocation_mode: str = "agent_invokes"

    # ── Schema v22: workspace file sync ──────────────────────────────────────
    # Spec: docs/spec-workspace-file-sync-2026-06-07.md.
    #
    # workspace_files_source — optional repo-root-relative path declaring
    #   where the manifest's bundled files live in the source tree. Used by
    #   apply_actions._sync_workspace_files to resolve the source dir when
    #   the package has no gallery files-pack (the side-loaded Atlas case).
    #   When set, sync walks manifest.files[] and copies any drifted file
    #   from {repo_root}/{workspace_files_source}/{path} into the bot's
    #   workspace at {workspace}/{path}. When empty, the resolver falls
    #   through to find_files_pack_dir(pkg_id); apps in gallery/<slug>/files/
    #   need not set this. Path-traversal escapes are rejected at resolve
    #   time so an operator-authored field can't reach outside the repo.
    #
    # workspace_sync — audit stamp written by sync at the end of each
    #   apply-actions run that touched files. Shape:
    #     {"last_synced_at": ISO, "source": "<origin label>",
    #      "synced_count": int, "drifted_paths": [...], "orphan_paths": [...],
    #      "missing_in_source": [...], "errors": [...]}
    #   Overwritten each run. Empty dict when sync has never run or had no
    #   source to resolve.
    workspace_files_source: str = ""
    workspace_sync: dict = field(default_factory=dict)

    # ── Schema v23: delivery_contract{} on scheduled_actions[] entries ──────
    # Spec: docs/spec-proactive-delivery-monitor-2026-06-10.md §5.
    #
    # No new top-level field — the optional ``delivery_contract`` block
    # lives INSIDE each ``scheduled_actions[]`` entry (sibling to
    # ``mechanism`` / ``install``). See the schema comment next to
    # ``DELIVERY_EVIDENCE_KINDS`` above for the full shape and
    # ``validate_delivery_contract`` for the canonical validator. Absence
    # means the delivery_monitor derives Option-A defaults; no migration
    # backfill is needed (the v23 bump is the version stamp only).
    #
    # v21 (bot_guidance / event_triggers / invocation_mode) and v22
    # (workspace_files_source / workspace_sync) shipped their dataclass
    # fields above without re-syncing MANIFEST_SCHEMA_VERSION (it sat at
    # 20); v23 is the next free number after them.

    # ── Schema v24: privacy{} + audience_scoping{} (manifest-v7 Slice 2) ─────
    # Spec: docs/spec-manifest-v7-slicing-2026-06-10.md §4; field shapes are
    # the base spec's §3 Atlas Gaps 3/4, pinned-structure / open-vocabulary
    # per base-spec §11.1 (mirrored by docs/schemas/manifest-v7-spec.schema.json).
    #
    #   privacy          — {user_data_collected: [str], opt_out_command: str,
    #                      consent_notice: str, retention_days: int>=1,
    #                      shareable_in_lessons: bool}. Machine-checkable
    #                      replacement for constraints.privacy prose.
    #                      `shareable_in_lessons` gates Lessons sharing
    #                      (lessons_share._spec_allows_lessons_share reads it
    #                      from the v7-arc Spec; absent → deny).
    #
    #   audience_scoping — {operator: operator_only|named_users|open,
    #                      approved_surfaces: [str], role_capabilities:
    #                      {role: [capability]}, operator_bypasses: [str]}.
    #                      Declares the trust boundary. event_triggers[].audience
    #                      must name a role_capabilities key once this block is
    #                      declared (privacy_scoping_validator).
    #
    # Empty dict = "not yet declared" — the validator only gates apps that
    # opt into the relevant behaviors (declared blocks, group-surface
    # triggers, Lessons sharing). No flag day; legacy manifests stay valid.
    # Enforcement beyond the trigger-audience check (gateway-level
    # role_capabilities) is deferred to the guard.py consolidation
    # (slicing spec §4.2).
    privacy: dict = field(default_factory=dict)
    audience_scoping: dict = field(default_factory=dict)

    # ── Schema v25/v26: purpose/fit classification (application/capability/system) ─
    # Spec: docs/applications-vs-skills.md (the definition) + the app-scan
    # purpose/fit classifier (bite D1; Slice 2 added the "system" verdict). The
    # scanner's LLM classifier labels each scanned manifest and stamps it here.
    #
    #   app_kind        — "application" | "capability" | "system" (v26). The
    #                     consumable label (the Apps page filters non-apps off
    #                     the grid on it, in the follow-on UI bite). INERT
    #                     DEFAULT "application": every existing/un-classified
    #                     manifest stays an app, so the over-route direction
    #                     (hiding a real app) never happens by default. A
    #                     capability/system label is only ever set by a
    #                     confident classifier verdict.
    #
    #   classification  — audit/idempotency block the classifier writes:
    #                     {kind, confidence, rationale, model_tier,
    #                      classified_by, classifier_version, raw_kind?}. Its
    #                      PRESENCE-AND-CURRENCY is the idempotency key: a
    #                      manifest whose block carries the current
    #                      classifier_version is skipped; one absent or stamped
    #                      by an older vocabulary is re-judged (scanner Phase
    #                      6.5 via purpose_classifier.needs_reclassification).
    #                      Empty dict = "not yet classified."
    #
    # Both default safe-empty/inert; no flag day, legacy manifests stay valid.
    app_kind: str = APP_KIND_APPLICATION
    classification: dict = field(default_factory=dict)

    # ── Schema v27: definition_status (Defined/Discovered source-of-truth axis) ─
    # Spec: docs/spec-apps-meta-2026-06-13.md §9. The third manifest axis,
    # distinct from ``status`` (lifecycle) and ``source`` (creation). See the
    # MANIFEST_DEFINITION_* / born_definition_status block above for the full
    # model. Inert default ``discovered``: a manifest with no explicit
    # born-status is the scanner-owned churnable draft — never accidentally
    # vouched or existence-shielded. New authored creations (forge/gallery/user)
    # stamp ``defined`` at their creation site; the migration of every existing
    # manifest lands at ``discovered`` (no bulk auto-promote — §9.1).
    definition_status: str = MANIFEST_DEFINITION_DISCOVERED

    # ── Schema v28: drift_log (drift-narrative log) — Bite 3 ──────────────────
    # Spec: docs/spec-apps-meta-2026-06-13.md §9.3 ("Drift is fluid — narrate,
    # don't gate"). Apps drift over time; the scanner rolls with it WITHOUT
    # per-change operator approval, keeping the manifest fresh AND giving the
    # operator a post-hoc NARRATIVE of significant ("major") drift to review.
    #
    #   drift_log — append-only list of MAJOR-drift entries the scanner's
    #               drift_classifier appends during the Phase-6 reconcile pass.
    #               Each entry (see drift_classifier.make_drift_entry):
    #                 {ts, kind: add|remove|modify, target_type, target,
    #                  significance: "major", summary, reviewed: bool,
    #                  source: "scanner_drift"}
    #               MINOR drift (data/doc, cosmetic) is absorbed silently and
    #               NEVER logged. Only ``defined`` manifests accrue entries — a
    #               ``discovered`` app is a churnable draft, so it just gets
    #               fresh content with no narrative (§9.3 / §9.4).
    #
    # WHY drift_log and not change_log: §9.3 names a "change_log[]", but the
    # v7-arc Instance ALREADY carries a load-bearing, schema-locked
    # ``change_log[]`` (the forge/capability audit trail compressed into Lessons
    # — strict additionalProperties:false, fixed ``kind`` enum, required
    # entry_id/who/description). The drift-entry shape (kind add|remove|modify +
    # significance + reviewed) is incompatible with it, and overloading it would
    # corrupt the Lessons pipeline. So the drift narrative ships as a NEW
    # distinct field, exactly the way v27 resolved the spec's "status" axis to
    # ``definition_status`` (spec §9.7). The existing ``change_log`` is untouched.
    #
    # Inert default []: a manifest with no drift narrative reads as "no major
    # drift observed." Readers branch on the field, never on the counter.
    drift_log: list[Any] = field(default_factory=list)

    # Legacy fields — kept for backward compat with reviewer / list commands
    version: int = 1
    schema_version: int = MANIFEST_SCHEMA_VERSION
    # v14 manifest_shape discriminator. Empty string = legacy single-file shape;
    # "v7-arc" = post-migration App Instance shape. Code paths that handle v7
    # artifacts branch on this field. Existing v13 manifests default to empty
    # via the migrate_manifest defaults dict.
    manifest_shape: str = MANIFEST_SHAPE_LEGACY
    priority: str = "feature"        # core | feature | optional
    success_metrics: list[Any] = field(default_factory=list)
    tests: list[Any] = field(default_factory=list)
    desired_improvements: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    approved_at: str = ""
    last_reviewed_at: str = ""

    def file_paths(self) -> list[str]:
        """
        Return file paths from the files registry regardless of format.

        v4 manifests store files as list[str] (plain paths).
        v5 manifests store files as list[dict] with full provenance records.
        This helper handles both transparently so callers don't need to branch.
        """
        paths = []
        for entry in (self.files or []):
            if isinstance(entry, str):
                paths.append(entry)
            elif isinstance(entry, dict):
                p = entry.get("path", "")
                if p:
                    paths.append(p)
        return paths

    def files_partition(
        self,
        files_pack_paths: set[str] | None = None,
    ) -> dict[str, list[str]]:
        """Partition ``files[]`` by provenance.

        Smart-forge dispatcher contract — see
        ``docs/note-smart-forge-and-file-provenance-2026-06-04.md``.

        Returns a dict with two keys:

          ``bundled``  → list of paths the install dispatcher should
                         copy from ``gallery/<slug>/files/`` (with
                         placeholder substitution).
          ``forge``    → list of paths the LLM-forge phase should
                         generate.

        Provenance is resolved per file:

          1. If the entry carries an explicit ``provenance`` key with
             value ``"bundled"`` or ``"forge"``, honour it.
          2. Else, INFER from whether the manifest has a stamped
             ``files_pack`` block AND the file's path is present in
             ``files_pack_paths`` (the set of paths declared in
             ``gallery/<slug>/files/manifest.json``). If both true →
             ``bundled``. Else → ``forge``.

        The inference rule keeps pre-existing manifests behaving
        exactly as they do today: a manifest WITHOUT a ``files_pack``
        block has every file classified as ``forge`` (so the existing
        LLM-forge path runs); a manifest WITH a ``files_pack`` block
        has every file in the pack classified as ``bundled`` (matches
        today's all-or-nothing behavior).

        Args:
          files_pack_paths: the set of paths declared in the files-pack
            metadata. Pass ``None`` to skip the path-membership check
            (then inferred provenance for all files is "forge").

        Returns:
          ``{"bundled": [...], "forge": [...]}``. Order matches
          ``self.files`` to keep deterministic iteration for callers.
        """
        has_files_pack = bool(self.files_pack or {})
        pack_paths = files_pack_paths or set()
        bundled: list[str] = []
        forge: list[str] = []
        for entry in (self.files or []):
            if isinstance(entry, str):
                path = entry
                explicit_prov = ""
            elif isinstance(entry, dict):
                path = (entry.get("path") or "").strip()
                explicit_prov = (entry.get("provenance") or "").strip()
            else:
                continue
            if not path:
                continue

            if explicit_prov == FILE_PROVENANCE_BUNDLED:
                bundled.append(path)
            elif explicit_prov == FILE_PROVENANCE_FORGE:
                forge.append(path)
            else:
                # Inference: bundled only when a files-pack exists AND
                # this path is actually in it.
                if has_files_pack and path in pack_paths:
                    bundled.append(path)
                else:
                    forge.append(path)
        return {"bundled": bundled, "forge": forge}

    def cron_dicts(self) -> list[dict]:
        """
        Return crons normalised to list[dict] regardless of stored format.

        v4 manifests store crons as raw crontab strings; v5+ store them as dicts.
        This helper handles both transparently so callers don't need to branch.

        Each returned dict always has: schedule, script, label, file_id.
        """
        return [_promote_cron(c) for c in (self.crons or [])]

    def cron_lines(self) -> list[str]:
        """
        Return crons as raw crontab-style strings ("schedule script"), suitable
        for comparison against live crontab output.
        """
        return [_cron_line(c) for c in (self.crons or [])]

    def is_gallery_app(self) -> bool:
        """Return True if this manifest was installed from the app gallery (has a pkg_id)."""
        return bool(self.pkg_id)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ApplicationManifest":
        known = set(cls.__dataclass_fields__)
        cleaned: dict[str, Any] = {k: v for k, v in data.items() if k in known}

        # Gallery JSONs and SpecDraft persistence carry tags under the key
        # `application_tags`; the manifest dataclass field is `tags`. The
        # alias keeps forge_engine's gallery→manifest seed path from
        # silently dropping tags. Only fires when `tags` itself isn't
        # already populated.
        if "tags" not in cleaned and isinstance(data.get("application_tags"), list):
            cleaned["tags"] = list(data["application_tags"])

        # Normalise legacy source values on load
        if "source" in cleaned and cleaned["source"] in _LEGACY_SOURCE_MAP:
            cleaned["source"] = _LEGACY_SOURCE_MAP[cleaned["source"]]

        # Deserialize legacy nested dataclasses tolerantly
        if "tests" in cleaned:
            deserialized: list[ApplicationTest] = []
            tc_fields = set(ApplicationTest.__dataclass_fields__)
            for t in cleaned["tests"]:
                if isinstance(t, dict):
                    try:
                        deserialized.append(
                            ApplicationTest(**{k: v for k, v in t.items() if k in tc_fields})
                        )
                    except Exception:
                        pass
            cleaned["tests"] = deserialized

        if "success_metrics" in cleaned:
            deserialized_m: list[SuccessMetric] = []
            sm_fields = set(SuccessMetric.__dataclass_fields__)
            for m in cleaned["success_metrics"]:
                if isinstance(m, dict):
                    try:
                        deserialized_m.append(
                            SuccessMetric(**{k: v for k, v in m.items() if k in sm_fields})
                        )
                    except Exception:
                        pass
            cleaned["success_metrics"] = deserialized_m

        return cls(**cleaned)


# ── Registry ──────────────────────────────────────────────────────────────────

def applications_dir(shared_dir: Path, bot_id: str) -> Path:
    """Per-bot manifest directory.

    Manifests are per-bot state — they describe an app's customization on
    one specific bot (file IDs, pkg_version, test history). They live with
    the bot at ``/Users/<bot>/.openclaw/workspace/manifests/``, alongside
    everything else the bot owns. Pod-level state (forge jobs, proposals,
    signals, gallery templates) stays in ``shared_dir``.

    The ``shared_dir`` parameter is kept in the signature for caller
    compatibility but is no longer used to resolve the path. The scanner
    grants ``evolve`` write ACL on this directory (running as the bot
    user during scan; admin-side writes use the ACL after that).
    """
    from ..config import get_bot_workspace
    workspace = get_bot_workspace(bot_id)
    if workspace is None:
        workspace = Path(f"/Users/{bot_id}/.openclaw/workspace")
    return workspace / "manifests"


# Backward-compatible alias
capabilities_dir = applications_dir


def list_manifests(shared_dir: Path, bot_id: str) -> list[ApplicationManifest]:
    d = applications_dir(shared_dir, bot_id)
    if not d.exists():
        return []
    manifests = []
    for f in sorted(d.iterdir()):
        # Skip dotfiles (e.g. ``.scan-status.json``) and underscore-prefixed
        # files (``_history/``). Without the dotfile filter, ``migrate_manifest``
        # would mutate ``.scan-status.json`` on every call by adding manifest
        # defaults (schema_version, identity, …) — observable corruption of
        # the scan-status payload on every bot's manifests/ dir.
        if not (f.suffix == ".json"
                and not f.name.startswith("_")
                and not f.name.startswith(".")):
            continue
        try:
            migrate_manifest(f)  # idempotent: only writes if schema needs updating
            data = json.loads(f.read_text())
            # v7-arc Instances don't carry name/description/tags — those live in
            # the Spec. Hydrate from the Spec so the admin UI's existing
            # rendering path keeps working without per-call branching.
            if data.get("manifest_shape") == MANIFEST_SHAPE_V7_ARC:
                data = hydrate_v7_arc_instance(data, shared_dir)
            manifests.append(ApplicationManifest.from_dict(data))
        except Exception:
            pass
    return manifests


def _merge_scheduled_action_delivery(
    instance_actions: list, spec_actions: list,
) -> list:
    """Overlay a Spec's user-facing delivery declaration onto an Instance's
    realized ``scheduled_actions[]``, per action id.

    The Instance entry wins on everything it declares (id, install, trigger,
    realized stamps); the Spec only fills a missing ``outputs[].channel``
    and a missing user-facing ``delivery_contract`` — the two fields scanner
    extraction (``quality="extracted"``) drops, which makes a gallery
    delivery invisible to ``delivery_monitor._derived_user_facing``. Returns
    a new list; never mutates the inputs.
    """
    by_id = {
        a.get("id"): a
        for a in spec_actions
        if isinstance(a, dict) and a.get("id")
    }
    merged: list = []
    for action in instance_actions:
        if not isinstance(action, dict):
            merged.append(action)
            continue
        spec_action = by_id.get(action.get("id"))
        if not isinstance(spec_action, dict):
            merged.append(action)
            continue
        new_action = dict(action)

        # outputs: fill only when the Instance declares no channel'd output.
        outs = new_action.get("outputs")
        has_channel = isinstance(outs, list) and any(
            isinstance(o, dict) and o.get("channel") for o in outs
        )
        spec_outs = spec_action.get("outputs")
        spec_has_channel = isinstance(spec_outs, list) and any(
            isinstance(o, dict) and o.get("channel") for o in spec_outs
        )
        if not has_channel and spec_has_channel:
            # deepcopy so a consumer mutating the hydrated action can't reach
            # back into the (shared) Spec dict.
            new_action["outputs"] = _copy.deepcopy(spec_outs)

        # delivery_contract: fill only when the Instance has no user-facing
        # one of its own.
        dc = new_action.get("delivery_contract")
        inst_user_facing = isinstance(dc, dict) and dc.get("user_facing") is True
        spec_dc = spec_action.get("delivery_contract")
        if not inst_user_facing and isinstance(spec_dc, dict):
            new_action["delivery_contract"] = _copy.deepcopy(spec_dc)

        merged.append(new_action)
    return merged


def hydrate_v7_arc_instance(instance: dict, shared_dir: Path) -> dict:
    """
    Synthesize a v13-shaped dict from a v7-arc Instance by overlaying its
    bound Spec's presentation fields (name, description, tags, objective,
    success_criteria). The Instance keeps its identity, realized_files,
    change_log, and lifecycle status; the Spec contributes everything the
    admin UI shows on cards.

    Looks up the Spec at:
        {shared_dir}/gallery/local/<spec_id>/<spec_version>.json
        {shared_dir}/gallery/imported/<source_pod_id>/<spec_id>/<spec_version>.json
        {shared_dir}/gallery/builtin/<spec_id>/<spec_version>.json

    If no Spec is found, returns the Instance unchanged — caller still gets a
    valid (if minimal) manifest dict.
    """
    provenance = instance.get("provenance") or {}
    spec_id = provenance.get("spec_id")
    spec_version = provenance.get("spec_version")
    if not spec_id or not spec_version:
        return instance

    # Try each tier; first found wins.
    gallery_root = shared_dir / "gallery"
    candidates: list[Path] = [
        gallery_root / "local" / spec_id / f"{spec_version}.json",
        gallery_root / "builtin" / spec_id / f"{spec_version}.json",
    ]
    source_pod_id = provenance.get("source_pod_id")
    if source_pod_id:
        candidates.insert(
            1,
            gallery_root / "imported" / source_pod_id / spec_id / f"{spec_version}.json",
        )

    spec: dict | None = None
    for p in candidates:
        if p.is_file():
            try:
                spec = json.loads(p.read_text())
                break
            except (OSError, json.JSONDecodeError):
                continue
    if spec is None:
        return instance

    # Start with Instance fields (keeps instance_id, realized_files, etc.).
    hydrated = dict(instance)

    # The UI's renderCapabilities + manifest dataclass key off `id` and `name`.
    # For v7-arc, `id` is the Instance filename stem (instance_id); the Spec
    # carries the human-facing name + description + tags.
    hydrated.setdefault("id", instance.get("instance_id", ""))

    # For v7-arc, provenance.spec_id IS the package identity (the migration
    # preserved conformant pkg_ids as spec_ids; native writes do the same).
    # Without this overlay every pkg_id consumer sees "" on v7-arc apps:
    # improvement-job creation falls back to the app slug, gallery
    # update/removal checks go blind, and the file index loses owned_by
    # attribution. Slice 3a (native writes) made fresh apps hit those paths
    # immediately, so the gap moved from latent to load-bearing.
    if not hydrated.get("pkg_id") and spec_id:
        hydrated["pkg_id"] = spec_id

    for key in ("name", "display_name", "description", "tags",
                "app_version", "approval_audience"):
        val = spec.get(key)
        if val:
            hydrated[key] = val

    # Spec's objective is a dict {primary, sub_objectives}; v13 manifest used a
    # string. Flatten to the primary for display.
    obj = spec.get("objective")
    if isinstance(obj, dict):
        hydrated["objective"] = obj.get("primary", "")
    elif isinstance(obj, str):
        hydrated["objective"] = obj

    # success_criteria carries through as-is (same dict shape).
    if spec.get("success_criteria"):
        hydrated["success_criteria"] = spec["success_criteria"]

    # `identity` is preserved from v13 in the Spec (purpose / scope_includes /
    # scope_excludes / user). UI's view modal renders the Identity section
    # from this block. Pass through verbatim so existing rendering works.
    if spec.get("identity"):
        hydrated["identity"] = spec["identity"]
    if spec.get("scope_excludes") and "scope_excludes" not in hydrated:
        hydrated["scope_excludes"] = spec["scope_excludes"]

    # S2.13 — additional v13 fields the migration now preserves on Spec.
    # Pass through to the UI so the view modal renders these sections.
    for key in ("constraints", "test_cases", "example_triggers",
                "owner", "inputs", "outputs"):
        if spec.get(key) and key not in hydrated:
            hydrated[key] = spec[key]

    # scheduled_actions: when the Instance declares none, take the Spec's
    # whole list (fill-if-missing, as for the keys above). When BOTH
    # declare actions, the Instance's realized entries win on identity /
    # install, but a scanner-extracted entry (quality="extracted") lands
    # with ``outputs: []`` and no ``delivery_contract`` — so the Spec's
    # user-facing delivery declaration is silently dropped and the action
    # falls out of delivery_monitor's monitored set (Atlas Daily Digest
    # silent non-delivery, 2026-06-16). Merge the Spec's
    # ``outputs[].channel`` + ``delivery_contract`` back per action id so an
    # extracted gallery delivery stays monitored.
    spec_actions = spec.get("scheduled_actions")
    if isinstance(spec_actions, list) and spec_actions:
        inst_actions = hydrated.get("scheduled_actions")
        if not isinstance(inst_actions, list) or not inst_actions:
            hydrated["scheduled_actions"] = spec_actions
        else:
            hydrated["scheduled_actions"] = _merge_scheduled_action_delivery(
                inst_actions, spec_actions,
            )

    # v7-arc trigger declarations live on the Spec (schedules[] /
    # event_triggers[]); overlay them so provenance-independent checks that
    # ask "does this app declare any recurring trigger?" (coherence Pass A
    # C-A1) see the Spec-side mechanism, not just the Instance-side
    # configured_schedules[]. Fill-if-missing, same as the rest.
    for key in ("schedules", "event_triggers"):
        if spec.get(key) and key not in hydrated:
            hydrated[key] = spec[key]

    # v24 — privacy{} + audience_scoping{} are Spec-owned facts (base-spec
    # §3 Atlas Gaps 3/4). Overlay them so the audit surface's data-boundary
    # projection (project_data_boundary) sees a migrated app's declared
    # blocks instead of reporting "not yet declared". Instance-local values
    # win when present (same fill-if-missing posture as the rest).
    for key in ("privacy", "audience_scoping"):
        if spec.get(key) and not hydrated.get(key):
            hydrated[key] = spec[key]

    # Derive a v13-style `files` list from realized_files so the existing
    # file_paths() helper and any consumer iterating .files keeps working.
    realized = instance.get("realized_files") or []
    if realized and not hydrated.get("files"):
        hydrated["files"] = [
            {
                "path": rf.get("path", ""),
                "description": rf.get("logical_name", ""),
                "file_id": rf.get("file_id", ""),
            }
            for rf in realized
            if isinstance(rf, dict)
        ]

    return hydrated


def get_app_dependents(pkg_id: str, bot_id: str, shared_dir: Path) -> list[dict]:
    """
    Return manifests on *bot_id* that declare *pkg_id* in their app_dependencies.

    Only returns manifests whose status is not 'hidden' or 'dormant' — inactive apps
    are not considered live dependents.

    Returns a list of dicts::
        [{"app_id", "name", "pkg_id", "required", "status"}, ...]
    """
    if not pkg_id:
        return []
    result: list[dict] = []
    for m in list_manifests(shared_dir, bot_id):
        if m.status in ("hidden", "dormant"):
            continue
        for dep in (m.app_dependencies or []):
            if dep.get("pkg_id") == pkg_id:
                result.append(
                    {
                        "app_id": m.id,
                        "name": m.display_name or m.name or m.id,
                        "pkg_id": m.pkg_id,
                        "required": dep.get("required", True),
                        "status": m.status,
                    }
                )
                break
    return result


def _write_manifest_bytes(path: Path, content: bytes) -> None:
    """Atomic write of a manifest JSON file to a bot-owned location.

    Strategy:
      1. Try direct ``path.write_bytes`` — succeeds when ``evolve`` already
         has write ACL on the parent dir (scanner grants this; see
         scanner.scan_workspace_pipeline). This is the common case.
      2. On PermissionError OR FileNotFoundError, fall back to ``/tmp``
         staging + ``sudo /bin/mkdir -p <parent> && sudo /bin/cp`` per
         CLAUDE.md's documented pattern for writing into bot-owned trees.
         The sudoers grant covers
         ``/tmp/evolve-manifest-*.json → /Users/*/.openclaw/workspace/manifests/*.json``.

    The first-run case — admin tries to save before the scanner has ever
    run on this bot — falls through to the sudo path. After that one run,
    direct writes work and the sudo path is dormant.

    FileNotFoundError caveat (atlas, 2026-05-28): on a fresh bot whose
    ``.openclaw/workspace/`` exists but ``.openclaw/workspace/manifests/``
    does NOT, the upstream ``save_manifest`` calls ``mkdir(d, parents=True,
    exist_ok=True)`` which fails silently with PermissionError (evolve can
    write to ``shared_dir`` but not to bot-owned ``workspace/``). The
    direct ``write_bytes`` then raises FileNotFoundError because the
    parent dir doesn't exist. Atlas's atlas-daily-digest forge run
    silently logged "Could not save updated manifest.files" 4 times,
    proceeded as if everything was fine, then failed at Step 10
    "Cannot approve: manifest not found". This module now treats both
    error shapes as a single "use sudo" trigger.
    """
    import tempfile
    import subprocess
    import os

    try:
        path.write_bytes(content)
        return
    except (PermissionError, FileNotFoundError):
        # PermissionError: ACL doesn't grant write yet (pre-scan)
        # FileNotFoundError: parent dir wasn't creatable by us, doesn't exist
        # Both → sudo fallback can fix.
        pass

    # Fallback: /tmp staging + sudo /bin/mkdir -p <parent> + sudo /bin/cp
    fd, tmp_path = tempfile.mkstemp(
        dir="/tmp", prefix="evolve-manifest-", suffix=".json"
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        # Ensure parent exists. Try the direct route first (works when
        # ACL allows but the dir is just missing); fall back to sudo
        # mkdir for the bot-owned case the atlas bug exposed.
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except (PermissionError, FileNotFoundError):
            mkdir_result = subprocess.run(
                ["sudo", "/bin/mkdir", "-p", str(path.parent)],
                capture_output=True, text=True, timeout=10,
            )
            if mkdir_result.returncode != 0:
                raise PermissionError(
                    f"manifest parent mkdir failed (rc="
                    f"{mkdir_result.returncode}): "
                    f"{mkdir_result.stderr.strip()[:200]}"
                )
        result = subprocess.run(
            ["sudo", "/bin/cp", tmp_path, str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            raise PermissionError(
                f"manifest write failed (direct EACCES + sudo cp rc="
                f"{result.returncode}): {result.stderr.strip()[:200]}"
            )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ── Schema v20: provenance stamping helpers ────────────────────────────────
#
# Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §4 + §13.1.
#
# PR 2 wires every manifest write path through ``stamp_field_origins`` so
# every field's authorship is recorded at write time. Downstream
# reconciliation (PR 4) reads ``provenance.field_origins.<field>.source``
# to decide whether a delta surfaces as a chip (authored fields) or
# silently updates (observational fields).
#
# The four valid sources mirror §4.1 of the spec:
#
#   observational  — scanner discovered this; no human ever vouched.
#                    Reconciliation silently overwrites.
#   forge_built    — forge materialised this from an operator-approved spec.
#                    Reconciliation surfaces as a chip.
#   user_authored  — operator edited via the manifest editor.
#                    Reconciliation surfaces as a chip.
#   bot_authored   — bot updated via an evo command on user instruction.
#                    Reconciliation surfaces as a chip.
#
# ``confirmed`` is a fifth source that arrives later via promotion (the
# operator clicks "Mark as ready" on an observational manifest). PR 2
# doesn't write ``confirmed`` directly; promotion is a separate code path
# in the admin UI handlers.

PROVENANCE_OBSERVATIONAL = "observational"
PROVENANCE_FORGE_BUILT   = "forge_built"
PROVENANCE_USER_AUTHORED = "user_authored"
PROVENANCE_BOT_AUTHORED  = "bot_authored"
PROVENANCE_CONFIRMED     = "confirmed"

VALID_PROVENANCE_SOURCES = frozenset({
    PROVENANCE_OBSERVATIONAL,
    PROVENANCE_FORGE_BUILT,
    PROVENANCE_USER_AUTHORED,
    PROVENANCE_BOT_AUTHORED,
    PROVENANCE_CONFIRMED,
})


# Sources that author new manifest content. Writes from these sources
# go through the apps-inherit-bot-llm gate in
# ``save_manifest_with_provenance``. ``observational`` is excluded so the
# scanner can re-stamp pre-rearchitect manifests during migration without
# the writes failing.
_APPS_INHERIT_BOT_LLM_GATED_SOURCES: frozenset[str] = frozenset({
    PROVENANCE_FORGE_BUILT,
    PROVENANCE_USER_AUTHORED,
    PROVENANCE_BOT_AUTHORED,
    PROVENANCE_CONFIRMED,
})


class ManifestPrincipleViolation(RuntimeError):
    """Raised when ``save_manifest_with_provenance`` refuses a write
    because the manifest violates a load-bearing Evolve design principle.

    The first principle gated this way is apps-inherit-bot-llm
    (docs/principle-apps-inherit-bot-llm.md). The exception carries
    enough structured context that the caller can render an actionable
    error in the forge job log, the admin UI, or the CLI.
    """

    def __init__(
        self,
        principle: str,
        *,
        source: str,
        manifest_id: str,
        bot_id: str,
        errors: list[str],
        message: str,
    ) -> None:
        super().__init__(message)
        self.principle = principle
        self.source = source
        self.manifest_id = manifest_id
        self.bot_id = bot_id
        self.errors = list(errors)
        self.message = message


def stamp_field_origins(
    manifest_dict: dict,
    *,
    source: str,
    fields: list[str] | None = None,
    by: str | None = None,
    via: str | None = None,
    at: str | None = None,
) -> list[tuple[str, str | None, str]]:
    """Stamp ``provenance.field_origins.<field>.source`` on a manifest dict.

    Spec: §4.3 (promotion paths) + §4.7 (provenance on the audit trail).

    Mutates ``manifest_dict`` in place. Returns a list of ``(field,
    previous_source, new_source)`` tuples for every entry that actually
    changed — callers use this to decide whether to emit a trail entry
    (no change → no entry, keeps the trail signal-rich).

    Args:
        manifest_dict: The manifest serialised as a dict. Must have a
            populated ``provenance`` block; if absent, this function
            creates the minimum shape.
        source: One of ``VALID_PROVENANCE_SOURCES``. Raises ``ValueError``
            on unknown values so silent provenance corruption can't
            happen.
        fields: Top-level field names to stamp. ``None`` means "every
            top-level key in the manifest" — used by forge after a full
            build, where it materialised everything. Pass an explicit
            list when only specific fields were touched (manifest editor
            saves, evo handlers, audit auto-fixes).
        by: Optional identifier for the actor — e.g. ``"operator"``,
            ``"forge:j-abc123"``, ``"evo:app-changes"``, ``"scanner"``.
            Stored on each touched ``field_origins`` entry for trail
            visibility.
        via: Optional channel the change arrived through — e.g.
            ``"manifest_editor"``, ``"evo"``, ``"scanner_phase_4"``.
        at: Optional ISO timestamp. Defaults to current UTC.

    Notes on semantics:

    * Fields already at the same source with the same ``by``/``via`` get
      no update (still returned as unchanged so the caller can skip the
      trail entry).
    * ``provenance.manifest_origin`` is left untouched — it represents
      the manifest's overall origin and only changes on explicit
      promotion (§4.6).
    * The set of skipped meta-fields mirrors ``_populate_provenance_v20``:
      ``schema_version``, ``created_at``, ``updated_at``, and the v20
      block keys (``provenance``, ``reconciliation``, ``coherence``,
      ``volatile_paths``) are never stamped — they're infrastructure,
      not authored content.
    """
    if source not in VALID_PROVENANCE_SOURCES:
        raise ValueError(
            f"unknown provenance source {source!r}; "
            f"valid: {sorted(VALID_PROVENANCE_SOURCES)}"
        )
    prov = manifest_dict.setdefault("provenance", {})
    field_origins = prov.setdefault("field_origins", {})
    at = at or now_iso()

    # Determine the field set. None = every top-level key minus meta.
    if fields is None:
        fields = list(manifest_dict.keys())

    _SKIP = {"provenance", "schema_version", "created_at", "updated_at",
             "reconciliation", "coherence", "volatile_paths"}

    changes: list[tuple[str, str | None, str]] = []

    for fld in fields:
        if fld in _SKIP:
            continue
        prior = field_origins.get(fld)
        prior_source = (prior or {}).get("source") if isinstance(prior, dict) else None
        prior_by = (prior or {}).get("by") if isinstance(prior, dict) else None
        prior_via = (prior or {}).get("via") if isinstance(prior, dict) else None
        if prior_source == source and prior_by == by and prior_via == via:
            # No-op stamp — same source, same actor, same channel.
            # Update ``at`` to mark "last touched" but don't record as a
            # change.
            if isinstance(prior, dict):
                prior["at"] = at
            continue
        new = {"source": source, "at": at}
        if by is not None:
            new["by"] = by
        if via is not None:
            new["via"] = via
        field_origins[fld] = new
        changes.append((fld, prior_source, source))

    return changes


def make_provenance_trail_entry(
    *,
    changes: list[tuple[str, str | None, str]],
    by: str | None,
    via: str | None,
    at: str | None = None,
) -> dict:
    """Build a ``provenance_change`` trail entry from a ``stamp_field_origins``
    result.

    The trail entry shape matches what the spec's §4.7 example shows: one
    record per write event, listing all fields whose authorship changed
    in that event. Multiple per-field changes batch into one trail entry
    so a "forge built 23 fields" event produces one trail line, not 23.

    Returns ``None`` if ``changes`` is empty — the caller decides whether
    to skip writing.

    The actual trail-write happens at the call site (the trail location
    varies by audience: per-bot audit trail for app-level writes,
    arbiter trail for proposal-level, etc.). This helper just builds the
    record.
    """
    if not changes:
        return None
    return {
        "ts": at or now_iso(),
        "kind": "provenance_change",
        "by": by,
        "via": via,
        "changes": [
            {"field": fld, "from": prior, "to": new}
            for fld, prior, new in changes
        ],
    }


def save_manifest(manifest: ApplicationManifest, shared_dir: Path) -> Path:
    d = applications_dir(shared_dir, manifest.bot_id)
    # mkdir is best-effort. If d is bot-owned and we're running as evolve
    # without the scanner having set the write ACL yet, mkdir fails — we
    # handle that below via the sudo fallback inside _write_manifest_bytes.
    # Both PermissionError (no ACL) and FileNotFoundError (parent missing
    # too — atlas fresh-bot case) need swallowing; the sudo fallback
    # mkdirs the parent before writing.
    try:
        d.mkdir(parents=True, exist_ok=True)
    except (PermissionError, FileNotFoundError):
        pass
    path = d / f"{manifest.id}.json"

    # Archive previous version if exists. Archive writes use the same
    # fallback strategy as the main write (direct first, sudo cp on
    # EACCES) — handled inside _write_manifest_bytes.
    if path.exists():
        archive_dir = d / "_history"
        try:
            archive_dir.mkdir(exist_ok=True)
        except (PermissionError, FileNotFoundError):
            pass
        try:
            old = json.loads(path.read_text())
        except (PermissionError, OSError):
            # If we can't read the prior version we still write the new
            # one — losing the archive entry beats losing the new write.
            old = None
        if old is not None:
            ts = old.get("approved_at", old.get("created_at", "unknown")).replace(":", "-")
            archive_path = archive_dir / f"{manifest.id}_v{old.get('version', 0)}_{ts}.json"
            _write_manifest_bytes(archive_path, json.dumps(old, indent=2).encode("utf-8"))

    _write_manifest_bytes(path, json.dumps(manifest.to_dict(), indent=2).encode("utf-8"))

    # Rebuild the global file index after any manifest change (best-effort)
    try:
        from .file_index import rebuild_file_index
        _rebuild_file_index_for(shared_dir)
    except Exception:
        pass

    return path


def save_manifest_with_provenance(
    manifest: ApplicationManifest,
    shared_dir: Path,
    *,
    source: str,
    fields: list[str] | None = None,
    by: str | None = None,
    via: str | None = None,
) -> Path:
    """``save_manifest`` + provenance stamping.

    Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md PR 2.

    The high-level wrapper every authored write path should call instead of
    bare ``save_manifest`` once PR 2 ships. Stamps ``field_origins`` for
    every listed field (or every top-level field when ``fields=None``),
    then persists.

    Callers map to source:
        scanner fresh-discovery write   → PROVENANCE_OBSERVATIONAL
        scanner re-stamp (Phase 5)      → PROVENANCE_OBSERVATIONAL
        forge build complete            → PROVENANCE_FORGE_BUILT
        forge re-materialise actions    → PROVENANCE_FORGE_BUILT
        manifest editor UI save         → PROVENANCE_USER_AUTHORED
        evo handler manifest modify     → PROVENANCE_BOT_AUTHORED
        operator "Mark as ready" click  → PROVENANCE_CONFIRMED

    The returned path is the same as ``save_manifest``.

    No trail entry is written here — provenance changes are recorded into
    the manifest's ``field_origins`` block (the spec's source of truth);
    the per-bot audit trail entry is the caller's choice (some writes
    are too frequent to log; some deserve a trail line). Callers that
    do want a trail entry can build one via ``make_provenance_trail_entry``
    on the ``changes`` returned by an explicit ``stamp_field_origins``
    call.

    apps-inherit-bot-llm gate: writes from sources that author new content
    (``forge_built``, ``user_authored``, ``bot_authored``, ``confirmed``)
    are validated against the principle and refused on violation. The
    ``observational`` source (scanner re-discovery) is allowed through so
    existing pre-rearchitect manifests can still be re-stamped during
    migration. See ``apps_inherit_bot_llm_validator``.
    """
    if source in _APPS_INHERIT_BOT_LLM_GATED_SOURCES:
        from .apps_inherit_bot_llm_validator import validate_apps_inherit_bot_llm
        result = validate_apps_inherit_bot_llm(manifest)
        if not result["ok"]:
            raise ManifestPrincipleViolation(
                "apps-inherit-bot-llm",
                source=source,
                manifest_id=getattr(manifest, "id", "unknown"),
                bot_id=getattr(manifest, "bot_id", "unknown"),
                errors=result["errors"],
                message=result["message"],
            )

    # Spec §6.6 pre-deploy coherence gate. Fires for operator-driven
    # writes that should refuse to ship something broken: admin UI
    # manifest editor saves (user_authored) and explicit Mark-as-ready
    # operations (confirmed). Bot-driven writes (bot_authored) and
    # observational re-stamps (scanner) are exempt — they re-record
    # reality rather than authoring intent. Forge approval has its own
    # explicit gate inside ``approve_forge_job`` (forge_built source is
    # already gated upstream).
    if source in (PROVENANCE_USER_AUTHORED, PROVENANCE_CONFIRMED):
        manifest_dict = (
            manifest.to_dict() if hasattr(manifest, "to_dict") else dict(manifest)
        )
        # The override_key can be threaded through via the manifest's
        # provenance.gate_override_key field — admin UI sets it when the
        # operator clicks "Override" on the chip. Absence = no override.
        prov = manifest_dict.get("provenance") or {}
        override = prov.get("gate_override_key")
        validate_coherence_gate(
            manifest_dict,
            bot_id=getattr(manifest, "bot_id", "") or "",
            app_id=getattr(manifest, "id", "") or "",
            override_key=override,
        )

    # Capture the prior on-disk snapshot before stamping/save so the C3
    # dispatch below can detect a charter_change (description /
    # usage.how_to_use / success_criteria.observable_outcomes edits).
    # Loading the prior manifest never raises — failure leaves
    # before_snapshot = None and the dispatch falls back to skipping.
    before_snapshot: dict | None = None
    if source in (PROVENANCE_USER_AUTHORED, PROVENANCE_BOT_AUTHORED):
        before_snapshot = _load_manifest_dict_from_disk(
            getattr(manifest, "id", ""),
            getattr(manifest, "bot_id", ""),
            shared_dir,
        )

    stamp_field_origins(
        manifest.__dict__ if not hasattr(manifest, "to_dict") else _manifest_view_for_stamping(manifest),
        source=source, fields=fields, by=by, via=via,
    )
    # ``stamp_field_origins`` mutates the dict view. If we used the
    # manifest's ``__dict__`` directly (the dataclass storage), the
    # provenance is now on the dataclass. If we used to_dict view, we
    # need to copy back. Current implementation uses __dict__, so the
    # dataclass is already updated.
    path = save_manifest(manifest, shared_dir)

    # ── Pass C3 LLM dispatch for operator-driven writes (spec §6.5) ─
    # When an operator or evo edits the manifest (user_authored /
    # bot_authored) AND a charter field actually changed, kick a C3
    # capability check so any subsequent pre-deploy gate has the
    # verdict to read. Skipped silently when rate-limited, structurally
    # incoherent, or the dispatcher module is unavailable. Forge writes
    # (forge_built) have their own dispatch in
    # ``forge_engine.approve_forge_job`` so we don't trigger twice.
    if source in (PROVENANCE_USER_AUTHORED, PROVENANCE_BOT_AUTHORED):
        _maybe_dispatch_c3_after_editor_save(
            manifest, shared_dir, source, before_snapshot,
        )

    return path


def _load_manifest_dict_from_disk(
    application_id: str, bot_id: str, shared_dir: Path,
) -> dict | None:
    """Read the raw manifest JSON before a save. Used to compute a
    before/after diff for the C3 charter-change trigger. Returns None
    when the manifest is new (no prior version on disk) or unreadable.
    """
    if not application_id or not bot_id:
        return None
    path = applications_dir(shared_dir, bot_id) / f"{application_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return None


def _maybe_dispatch_c3_after_editor_save(
    manifest: "ApplicationManifest",
    shared_dir: Path,
    source: str,
    before_snapshot: dict | None,
) -> None:
    """Best-effort C3 dispatch following an operator/evo manifest save.

    Skips quietly when:
      - the dispatcher / Pass A / C3 module fails to import
      - C3's cached verdict is still inside the 24h rate-limit window
      - no charter field changed since the prior on-disk snapshot
      - Pass A reports ``incoherent`` (no point burning $$ on a manifest
        whose structural gate is about to block anyway)
      - the LLM call itself errors (e.g. no API key configured)

    Mutates the manifest's coherence block on success via the dispatcher
    but does not re-persist — ``save_manifest`` already ran *and* the
    dispatcher itself calls ``save_manifest`` after writing the verdict.
    The next reader of the file (the pre-deploy gate, the evo handler,
    the audit tile) picks up the verdict from disk.
    """
    bot_id = getattr(manifest, "bot_id", "") or ""
    app_id = getattr(manifest, "id", "") or ""
    if not bot_id or not app_id:
        return
    try:
        from .coherence_c3_dispatcher import dispatch_c3
        from .coherence_pass_c3 import is_rate_limited as _c3_rate_limited
        from .coherence_pass_a import run_pass_a, status_for_findings
    except Exception:  # noqa: BLE001
        return

    try:
        manifest_dict = (
            manifest.to_dict() if hasattr(manifest, "to_dict") else dict(manifest)
        )
    except Exception:  # noqa: BLE001
        return

    if _c3_rate_limited(manifest_dict):
        return

    try:
        a_status = status_for_findings(run_pass_a(manifest_dict))
    except Exception:  # noqa: BLE001
        a_status = "ok"
    if a_status == "incoherent":
        return

    try:
        dispatch_c3(
            bot_id=bot_id, app_id=app_id, trigger="charter_change",
            shared_dir=shared_dir,
            before_manifest=before_snapshot,
        )
    except Exception:  # noqa: BLE001
        # Editor save must not fail because the LLM dispatch errored.
        return


def _manifest_view_for_stamping(manifest: ApplicationManifest) -> dict:
    """Return the ApplicationManifest's ``provenance`` dict (and the keys
    needed by stamp_field_origins for the 'all fields' fast path).

    Stamping mutates the dataclass's ``provenance`` attribute directly so
    the change survives the next ``to_dict()`` serialisation. We can't
    use ``asdict(manifest)`` here because that returns a copy — stamping
    a copy would lose the change. So we wrap the dataclass's actual
    ``provenance`` dict with a view that exposes the rest of the fields
    read-only (stamp_field_origins reads ``manifest_dict.keys()`` to know
    what to stamp when ``fields=None``).
    """
    # Build a small dict that proxies the dataclass's fields. provenance is
    # the mutable target; everything else is exposed read-only via
    # ``__getattr__``-style key access (just dict.keys()).
    view: dict = {k: getattr(manifest, k) for k in manifest.__dataclass_fields__}
    # Replace the provenance field in the view with the dataclass's actual
    # dict so mutations propagate.
    view["provenance"] = manifest.provenance
    return view


def _rebuild_file_index_for(shared_dir: Path) -> None:
    """Discover all bot_ids by scanning shared_dir/applications/ and rebuild the index."""
    from .file_index import rebuild_file_index
    apps_dir = shared_dir / "applications"
    if not apps_dir.exists():
        return
    bot_ids = [d.name for d in apps_dir.iterdir() if d.is_dir()]
    rebuild_file_index(shared_dir, bot_ids)


_DELETE_PRESERVE_LAYERS = {"data", "state"}


def unwire_event_triggers(
    application_id: str,
    bot_id: str,
    shared_dir: Path,
) -> dict:
    """Unregister the app's ``event_triggers[]`` from the plugin interceptor
    (base-spec §8.4 step 3; manifest-v7 Slice 1, spec-manifest-v7-slicing §3.2).

    The manifest JSON *is* the registration: the plugin's Layer C compiles
    triggers straight from ``/Users/<bot>/.openclaw/workspace/manifests/``.
    Final manifest deletion therefore unregisters too — but the uninstall
    sequence unlinks the app's script files BEFORE the manifest goes (and a
    failed unlink leaves the manifest behind indefinitely as the resumable
    checklist). In that window the plugin keeps intercepting matched
    messages and invoking now-deleted scripts, posting fallback text for an
    app the operator believes removed. Run this FIRST in any uninstall
    sequence to close the window.

    Destructive (clears ``event_triggers`` and downgrades
    ``invocation_mode``) — only call on the uninstall path, where the
    manifest is going away anyway. A reversible pause/deprecate unwire
    needs a trigger-stash field (schema addition; later slice).

    The rewrite goes through temp-file + rename in the manifests dir
    itself: the plugin invalidates its trigger cache on the *directory*
    mtime, which an in-place write never bumps — the unwire would
    otherwise sit unread until the next dir-entry change.

    Returns ``{ok, unwired, trigger_count}`` (+ ``error`` on failure).
    ``unwired=False`` with ``ok=True`` means there was nothing to unwire.
    """
    import os
    import tempfile

    path = applications_dir(shared_dir, bot_id) / f"{application_id}.json"
    if not path.exists():
        return {"ok": False, "unwired": False, "trigger_count": 0,
                "error": "manifest not found"}
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        return {"ok": False, "unwired": False, "trigger_count": 0,
                "error": f"could not read manifest: {exc}"}

    triggers = data.get("event_triggers") or []
    trigger_count = len(triggers) if isinstance(triggers, list) else 1
    intercepting = data.get("invocation_mode") == "plugin_intercept"
    if not triggers and not intercepting:
        return {"ok": True, "unwired": False, "trigger_count": 0}

    data["event_triggers"] = []
    if intercepting:
        data["invocation_mode"] = "agent_invokes"
    content = json.dumps(data, indent=2).encode()

    tmp: str | None = None
    try:
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{application_id}-unwire-", suffix=".tmp"
        )
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.replace(tmp, path)
    except (PermissionError, OSError):
        # No write ACL on the manifests dir (pre-scan bot) — fall back to
        # the sudo-cp path. The dir mtime won't bump, so a running gateway
        # serves stale triggers until the final manifest deletion; still
        # better than leaving the registration in the JSON.
        if tmp is not None and os.path.exists(tmp):
            # mkstemp succeeded, so we hold dir-write permission and this
            # same-dir unlink can't be what failed above.
            os.unlink(tmp)
        try:
            _write_manifest_bytes(path, content)
        except Exception as exc:
            return {"ok": False, "unwired": False, "trigger_count": trigger_count,
                    "error": f"could not rewrite manifest: {exc}"}

    return {"ok": True, "unwired": True, "trigger_count": trigger_count}


def plan_manifest_deletion(
    application_id: str,
    bot_id: str,
    shared_dir: Path,
    workspace_path: Path | None = None,
) -> dict:
    """Pure planner — partitions the manifest's files into preserved /
    cleaned / deletion_candidates without mutating anything on disk.

    The manifest is the natural checklist for uninstall. Callers should:
      1. plan_manifest_deletion(...)            ← what would happen
      2. unwire_event_triggers(...)             ← FIRST mutation: unregister
                                                  plugin-interceptor triggers
      3. apply_manifest_marker_cleanup(...)     ← strip pkg_id from markers
      4. (caller) unlink confirmed files
      5. finalize_manifest_deletion(...)        ← LAST: delete manifest JSON

    Deleting the manifest last means a mid-flight failure leaves it on
    disk as resumable state — the operator's clean-up flow has not lost
    its reference. The historical ``delete_manifest`` flow deleted the
    manifest first and left orphan files when the unlink loop never ran
    (personal-bot / task-manager, 2026-06-02).

    Returns the same shape as :func:`delete_manifest` did, minus ``ok``
    being conditional on actual deletion: here, ``ok=True`` only means
    "manifest could be loaded and partitioned."
    """
    manifest = load_manifest(application_id, bot_id, shared_dir)
    if manifest is None:
        return {"ok": False, "error": "manifest not found"}

    pkg_id = manifest.pkg_id
    preserved: list[str] = []
    cleaned: list[str] = []
    candidates: list[str] = []

    if workspace_path is not None:
        for entry in (manifest.files or []):
            if isinstance(entry, str):
                continue  # v4 path-only entries — no marker cleanup possible
            if not isinstance(entry, dict):
                continue

            rel_path = entry.get("path", "")
            layer = entry.get("layer", "")
            shared_with = entry.get("shared_with") or []
            owned_by = entry.get("owned_by", pkg_id)

            if not rel_path:
                continue

            if layer in _DELETE_PRESERVE_LAYERS:
                preserved.append(rel_path)
            elif shared_with or owned_by != pkg_id:
                cleaned.append(rel_path)
            else:
                candidates.append(rel_path)

    return {
        "ok": True,
        "pkg_id": pkg_id,
        "preserved_files": preserved,
        "cleaned_files": cleaned,
        "deletion_candidates": candidates,
    }


def apply_manifest_marker_cleanup(
    application_id: str,
    bot_id: str,
    shared_dir: Path,
    workspace_path: Path,
    plan: dict | None = None,
) -> dict:
    """Strip this manifest's pkg_id from every owned/shared file marker.

    Does NOT delete the manifest JSON and does NOT unlink any source
    files. Idempotent and best-effort: per-file marker failures are
    swallowed (they shouldn't block the uninstall).

    If ``plan`` is provided it's used as the partition; otherwise the
    function computes one. Returns the plan dict it operated on so
    callers can reuse it for the file-unlink and finalize steps.
    """
    from .provenance import remove_pkg_id_from_marker

    if plan is None:
        plan = plan_manifest_deletion(
            application_id, bot_id, shared_dir, workspace_path
        )
    if not plan.get("ok"):
        return plan

    pkg_id = plan.get("pkg_id", "")
    if not pkg_id:
        return plan

    for rel in plan.get("preserved_files", []) + plan.get("cleaned_files", []) + plan.get("deletion_candidates", []):
        fpath = workspace_path / rel
        if not fpath.exists():
            continue
        try:
            remove_pkg_id_from_marker(fpath, pkg_id)
        except Exception:
            pass

    return plan


def finalize_manifest_deletion(
    application_id: str,
    bot_id: str,
    shared_dir: Path,
) -> dict:
    """Archive + delete the manifest JSON, then rebuild the file index.

    Run this LAST in an uninstall sequence — every other step (cron
    teardown, scheduled-action removal, file unlink) should read the
    manifest first. Until this call succeeds, the manifest is still
    on disk and the operator can resume cleanly after a failure.
    """
    from .file_index import rebuild_file_index

    manifest_path = applications_dir(shared_dir, bot_id) / f"{application_id}.json"
    if not manifest_path.exists():
        return {"ok": False, "error": "manifest not found"}

    try:
        archive_dir = applications_dir(shared_dir, bot_id) / "_history"
        try:
            archive_dir.mkdir(exist_ok=True)
        except (PermissionError, FileNotFoundError):
            pass
        ts = now_iso().replace(":", "-")
        archive_path = archive_dir / f"{application_id}_deleted_{ts}.json"
        try:
            import shutil as _shutil
            _shutil.copy2(str(manifest_path), str(archive_path))
        except Exception:
            pass
        manifest_path.unlink()
    except Exception as exc:
        return {"ok": False, "error": f"could not delete manifest file: {exc}"}

    try:
        apps_dir = shared_dir / "applications"
        bot_ids = [d.name for d in apps_dir.iterdir() if d.is_dir()] if apps_dir.exists() else []
        rebuild_file_index(shared_dir, bot_ids)
    except Exception:
        pass

    return {"ok": True}


def delete_manifest(
    application_id: str,
    bot_id: str,
    shared_dir: Path,
    workspace_path: Path | None = None,
) -> dict:
    """
    Remove a manifest and clean up its component files' provenance markers.

    Deletion is layer-aware:
    - ``data`` and ``state`` layer files are **always preserved** — their markers are
      stripped (they become unowned) but the files themselves are not touched.
    - All other layers (script, skill, policy, orchestrator, test, reference) have
      the owning pkg_id removed from their marker.  If no other app owns the file
      after removal, it is added to ``deletion_candidates`` for the caller to handle
      (the caller decides whether to actually delete or leave them).
    - Files shared with other apps just lose this app's pkg_id from their marker;
      the file itself stays fully intact.

    The manifest JSON is deleted as the final step, after marker cleanup,
    so a partial failure leaves the manifest on disk as a resumable
    checklist for the operator. The global file index is rebuilt last.

    Used by forge_jobs to roll back never-completed installs. The admin
    API (``delete_application``) uses the three-step split — plan +
    apply markers + (operator-confirmed file unlink) + finalize — so it
    can interleave file deletion between the markers and the manifest.

    Args:
        application_id: The manifest slug (e.g. ``"health-tracking"``).
        bot_id:         The owning bot.
        shared_dir:     Shared evolve data directory.
        workspace_path: Root of the bot's workspace.  Required for marker cleanup;
                        if None, marker cleanup is skipped.

    Returns:
        Dict with keys:
            ok              : bool
            pkg_id          : str   — the app's pkg_id (for audit logging)
            preserved_files : list  — paths preserved (data/state layer)
            cleaned_files   : list  — paths whose markers were updated (shared → still live)
            deletion_candidates : list  — paths that are now fully unowned (safe layers only)
            triggers_unwired : dict — result of unwire_event_triggers (§8.4 step 3)
            error           : str   — present only on failure
    """
    plan = plan_manifest_deletion(
        application_id, bot_id, shared_dir, workspace_path
    )
    if not plan.get("ok"):
        return plan

    # §8.4 step 3: unregister plugin-interceptor triggers BEFORE any file
    # leaves disk, so a mid-flight failure can't leave live triggers
    # pointing at deleted scripts. Best-effort — the finalize step below
    # unregisters too (by deleting the manifest), so a failed unwire is
    # reported but doesn't block the uninstall.
    plan["triggers_unwired"] = unwire_event_triggers(
        application_id, bot_id, shared_dir
    )

    if workspace_path is not None:
        apply_manifest_marker_cleanup(
            application_id, bot_id, shared_dir, workspace_path, plan=plan
        )

    finalized = finalize_manifest_deletion(application_id, bot_id, shared_dir)
    if not finalized.get("ok"):
        return finalized

    return plan


def load_manifest(application_id: str, bot_id: str, shared_dir: Path) -> ApplicationManifest | None:
    path = applications_dir(shared_dir, bot_id) / f"{application_id}.json"
    if not path.exists():
        return None
    try:
        migrate_manifest(path)  # idempotent: only writes if schema needs updating
        data = json.loads(path.read_text())
        # v7-arc Instances don't carry id/name; they're hydrated from the
        # bound Spec (same as list_manifests). Without this, from_dict raises
        # on the missing required fields and the caller — e.g. delete_manifest
        # via DELETE /api/applications/<bot>/<app> — sees "manifest not found"
        # for an Instance the operator can see in the UI.
        if data.get("manifest_shape") == MANIFEST_SHAPE_V7_ARC:
            data = hydrate_v7_arc_instance(data, shared_dir)
        return ApplicationManifest.from_dict(data)
    except Exception:
        return None


def _migrate_scheduled_action_entry_v20(entry: dict) -> bool:
    """Add v20 fields to one scheduled_actions[] entry.

    Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §3.4.

    Three additions, all defensive defaults:

    * ``state`` defaults to ``"active"``. Existing entries are assumed to
      be active unless they say otherwise. Operators mark entries as
      ``"disabled"`` (intentional pause, e.g. personal-bot's protein-daily-checkin)
      or ``"paused"`` (with a ``paused_until`` date) via the manifest
      editor.
    * ``quality`` is the most important migration here. Entries whose
      ``mechanism`` is ``"unknown"`` (the v16 default for everything the
      scanner couldn't attribute) are auto-marked ``"suspect"``. personal-bot's
      and team-bot-a's manifests today contain many such entries — first-line
      excerpts of AGENTS.md sections captured as if they were scheduled
      behaviors (e.g., ``summary: "1. Read SOUL.md — who you are"``).
      Coherence Pass A is required to skip suspect entries to avoid
      flooding operators with false-positive findings. Entries with a
      real mechanism get ``"extracted"`` — they came from the scanner
      but the mechanism resolution suggests they're substantive.
      Operator confirmation (via the manifest editor or via a fresh
      scan with the tightened prompt) promotes to ``"verified"``.
    * ``safety_net_for`` defaults to an empty list. The field links one
      scheduled_action to another it monitors — the load-bearing pattern
      from the protein-reminder failure (heartbeat clobber kills
      heartbeat-only critical work). Empty by default; operator opts in
      by populating.

    Idempotent: returns ``True`` iff the entry changed.
    """
    changed = False
    if "state" not in entry:
        entry["state"] = "active"
        changed = True
    if "quality" not in entry:
        mech = (entry.get("mechanism") or "").strip().lower()
        # MECHANISM_UNKNOWN is the v16 "scanner couldn't attribute" sentinel.
        # Anything else means the scanner reached an opinion about how the
        # action was installed; that opinion is trustworthy for structure
        # even if substance still needs verification.
        if not mech or mech == MECHANISM_UNKNOWN:
            entry["quality"] = "suspect"
        else:
            entry["quality"] = "extracted"
        changed = True
    if "safety_net_for" not in entry:
        entry["safety_net_for"] = []
        changed = True
    return changed


def _migrate_file_entry_v20(entry: dict) -> bool:
    """Map legacy layer values on one files[] entry.

    Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §3.1.

    The pre-v20 stamper used ``"script"`` for code files; v20 standardises
    on ``"code"`` so the layer enum is internally consistent. When we
    remap, ``_legacy_layer`` captures the original value so the trail can
    show the migration history.

    Note: this migration does NOT re-classify mis-classified entries —
    personal-bot's content files stamped ``"state"`` instead of ``"content"`` need
    the full classifier pass from PR 3 to fix. This migration is a
    pure rename for the one ambiguity that has a clean mapping.

    Idempotent: returns ``True`` iff the entry changed.
    """
    layer = entry.get("layer")
    if layer == "script":
        entry["layer"] = "code"
        # Preserve original for trail visibility; allows PR 3's
        # classifier to know it was an automatic rename, not an
        # operator-set value.
        if "_legacy_layer" not in entry:
            entry["_legacy_layer"] = "script"
        return True
    return False


def _populate_provenance_v20(data: dict) -> bool:
    """Populate the v20 ``provenance`` block from existing manifest state.

    Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §4 +
    §13.1.

    Migration policy (§13.1): every existing top-level field defaults to
    ``"observational"``. The exception is manifests that were forge-built
    — detected by a non-empty ``build_spec`` (forge writes this when it
    materialises an app from an operator-approved spec). For those, every
    field defaults to ``"forge_built"``.

    The safe default is observational — it errs on the side of "no chip"
    until the operator actively promotes. Per-field granularity per the
    spec is a v2 evolution; v20 uses uniform per-manifest origin.

    Idempotent: returns ``True`` iff the block was changed.
    """
    prov = data.get("provenance")
    if not isinstance(prov, dict):
        prov = {}
        data["provenance"] = prov

    changed = False

    # Determine manifest_origin from existing signals. Per §13.1: the
    # safe default is observational; only mark forge_built when there's
    # a definitive signal.
    #
    # We DO NOT use ``build_spec`` here — empirically (validation against
    # personal-bot's app-daily-cost-check.json) the scanner stuffs generated
    # build documentation into this field, so a populated value doesn't
    # imply forge involvement. The header docstring claims ``build_spec``
    # is a forge provenance field but reality drifted.
    #
    # The reliable signals are:
    #   1. ``source == "forge_built"`` — explicit canonical mark
    #   2. ``install_job`` non-empty — forge sets this when dispatching install
    source = (data.get("source") or "").strip().lower()
    install_job = data.get("install_job")
    if source == "forge_built" or install_job:
        manifest_origin = "forge_built"
    else:
        manifest_origin = "observational"

    if prov.get("manifest_origin") != manifest_origin:
        # Only write if missing — never overwrite an existing value.
        # An operator may have manually marked manifest_origin already.
        if not prov.get("manifest_origin"):
            prov["manifest_origin"] = manifest_origin
            changed = True

    # Derive created_by from source (scanner / forge / operator / bot).
    if not prov.get("created_by"):
        created_by = "scanner"  # safe default
        if source == "forge_built":
            created_by = "forge"
        elif source == "user_created":
            created_by = "operator"
        elif source == "discovered":
            created_by = "scanner"
        prov["created_by"] = created_by
        changed = True

    # Copy created_at from manifest if available.
    if not prov.get("created_at"):
        prov["created_at"] = data.get("created_at") or ""
        changed = True

    # last_promoted_at stays null on first migration; operator promotion
    # writes this field directly.
    if "last_promoted_at" not in prov:
        prov["last_promoted_at"] = None
        changed = True

    # Stamp every existing top-level field's origin. v20 uses uniform
    # per-manifest origin (manifest_origin); per-field granularity is
    # a v2 evolution.
    field_origins = prov.get("field_origins")
    if not isinstance(field_origins, dict):
        field_origins = {}
        prov["field_origins"] = field_origins
        changed = True

    # Skip meta fields and the provenance block itself.
    _SKIP_FIELDS = {"provenance", "schema_version", "created_at",
                    "updated_at", "reconciliation", "coherence",
                    "volatile_paths"}

    for key in data.keys():
        if key in _SKIP_FIELDS:
            continue
        if key in field_origins:
            continue  # already stamped
        field_origins[key] = {"source": manifest_origin}
        changed = True

    return changed


def _migrate_scheduled_action_entry_v16(entry: dict) -> bool:
    """Add v16 install-metadata sub-fields to one scheduled_actions[] entry.

    Idempotent: returns ``True`` iff the entry changed. Pre-v16 entries get
    ``mechanism = "unknown"`` and an empty ``install`` recipe; scanner
    attribution (spec-forge-side-effects §6) rewrites ``mechanism`` once
    it can resolve the install site. The four ``installed_*`` provenance
    fields stay absent (forge stamps them at install time).
    """
    changed = False
    if "mechanism" not in entry:
        entry["mechanism"] = MECHANISM_UNKNOWN
        changed = True
    if "install" not in entry:
        entry["install"] = {}
        changed = True
    # installed_at / installed_by / installed_artifact / last_verified are
    # provenance, not state — leave them missing rather than stamping
    # placeholder Nones. Consumers should default to None on .get().
    return changed


def _migrate_scheduled_action_entry_v17(entry: dict) -> bool:
    """Rewrite deprecated mechanism values to their v17 replacements.

    Spec: docs/spec-heartbeat-instruction-2026-06-03.md §2.1, §9.

    The pre-v17 ``oc_heartbeat_hook`` and ``oc_session_hook`` values were
    built on a wrong assumption (OpenClaw has no ``hooks.heartbeat[]``
    array). Re-installing under the new mechanism produces a different
    artifact (``HEARTBEAT.md#Section`` vs ``openclaw.json#hooks.heartbeat``),
    so we also clear ``installed_artifact`` (and the install timestamp)
    to force the next forge run to materialize via the correct surface.

    Idempotent: returns ``True`` iff the entry changed.
    """
    mech = (entry.get("mechanism") or "").strip()
    if mech not in _DEPRECATED_MECHANISM_REWRITES:
        return False
    entry["mechanism"] = _DEPRECATED_MECHANISM_REWRITES[mech]
    # Clear provenance so the next forge run installs to the right place.
    # Leave ``installed_by`` for audit trail visibility — operators can
    # see "previously installed by forge:j-XXX under oc_heartbeat_hook".
    for field in ("installed_artifact", "installed_at"):
        if field in entry:
            entry[field] = None
    return True


def _normalize_manifest_shape(data: dict) -> bool:
    """Coerce a corrupt ``manifest_shape`` discriminator back to a canonical value.

    ``manifest_shape`` is a closed enum (``VALID_MANIFEST_SHAPES``). A value
    outside the set — most often a schema-version string like ``"v20"`` that
    leaked in from an LLM-driven manifest synthesis or a hand edit — silently
    orphans the manifest: the legacy→v7-arc promotion path skips it (shape
    != ``""``) AND every v7/Reflect reader skips it (shape != ``"v7-arc"``), so
    it is invisible to both. Self-heal on the next migrate pass by inferring the
    true shape from structure:

      * v7-arc Instance   → carries ``instance_id`` and/or ``realized_files``
      * legacy single-file → everything else (inline ``files``, top-level id/name)

    We never *infer* the transient ``"v7-arc-pre"`` intermediate — a corrupt
    value is never legitimately mid-gallery-install — but a manifest already
    stamped ``"v7-arc-pre"`` is canonical and left untouched.

    Returns True if the value was changed (caller marks the manifest dirty).
    """
    shape = data.get("manifest_shape")
    if shape in VALID_MANIFEST_SHAPES:
        return False
    inferred = (
        MANIFEST_SHAPE_V7_ARC
        if (data.get("instance_id") or data.get("realized_files"))
        else MANIFEST_SHAPE_LEGACY
    )
    # Breadcrumb in the scanner / admin-server log so the heal is observable.
    import sys

    print(
        f"[manifest] non-canonical manifest_shape {shape!r} "
        f"(id={data.get('id') or data.get('instance_id')!r}); "
        f"coercing to {inferred!r} based on structure",
        file=sys.stderr,
        flush=True,
    )
    data["manifest_shape"] = inferred
    return True


def migrate_manifest(path: Path) -> None:
    """Add missing schema v2 fields to an existing thin manifest with defaults.

    Refuses to mutate anything that doesn't look like a manifest. A manifest
    has at least an ``id`` (canonical) or a ``manifest_type`` discriminator.
    Without this guard, calling migrate on ``.scan-status.json`` would
    silently fill it with default manifest fields and rewrite it.
    """
    if path.name.startswith(".") or path.name.startswith("_"):
        # Dotfiles (``.scan-status.json``) and underscore-prefixed files
        # (``_history/<…>``) are scanner state, never manifests.
        return
    try:
        data = json.loads(path.read_text())
    except Exception:
        return
    if not isinstance(data, dict):
        return
    if not (data.get("id") or data.get("manifest_type") == "evolve_application"):
        # File at this path is not a manifest — leave it alone.
        return

    changed = False
    # Normalise legacy source values to the new canonical form
    if "source" in data and data["source"] in _LEGACY_SOURCE_MAP:
        data["source"] = _LEGACY_SOURCE_MAP[data["source"]]
        changed = True

    # Ensure timestamps are populated
    _now = now_iso()
    if not data.get("created_at"):
        data["created_at"] = _now
        changed = True
    if not data.get("updated_at"):
        data["updated_at"] = data["created_at"]
        changed = True

    defaults: dict[str, Any] = {
        "status": "active",
        "source_detail": "",
        "purpose": "",
        "goals": [],
        "example_triggers": [],
        # test_cases / last_test_* / test_command / test_cadence /
        # test_exemption_reason removed from migrate defaults 2026-06-08 —
        # app-test surface killed per docs/decision-app-tests-2026-06-08.md.
        # Existing on-disk values are preserved by the dataclass; new
        # manifests no longer acquire empty placeholders.
        "privacy_constraints": [],
        "satisfaction_notes": None,
        "improvement_history": [],
        "known_issues": [],
        "open_questions": [],
        "tags": [],
        "schema_version": MANIFEST_SCHEMA_VERSION,
        # v3: 4-section RSI fields
        "identity": {},
        "success_criteria": {},
        "constraints": {},
        "satisfaction": {"score": None, "notes": None, "rated_at": None},
        # v4: operational/registry fields
        "app_version": "",
        "objective": "",
        "owner": "",
        "maintainers": [],
        "files": [],
        "crons": [],
        "inputs": [],
        "outputs": [],
        "exported_hooks": [],
        "rsigrade_signals": [],
        "docs": [],
        "last_reviewed": "",
        "last_verification": {},
        "compliance_suppressed": False,
        "compliance_suppressed_reason": "",
        # v5: gallery & forge provenance fields
        "pkg_id": "",
        "pkg_version": "",
        "gallery_version": "",
        "display_name": "",
        "author": "",
        "build_spec": "",
        "install_job": None,
        "dependencies": [],
        # v6: dependency resolution & interface contract
        "app_dependencies": [],
        "requirements": {},
        "interface_contract": {},
        # v7: session attribution hints
        "capability_tags": [],
        "session_keywords": [],
        # v10: bot-facing usage manual
        "usage": {},
        # v11: app-audit telemetry (Tier 2)
        "last_structural_verify": {},
        "audit_trail_path": "",
        # v12: Tier-3 semantic audit fields
        "audit_cadence": None,
        "audit_eligible": True,
        "audit_accepted": [],
        "last_audit": {},
        # v13: scheduled-action contracts (extracted from heartbeats / crons)
        "scheduled_actions": [],
        "heartbeat_evidence": {},
        "cron_evidence": {},
        # v14: manifest_shape discriminator. Empty = legacy single-file shape;
        # set to "v7-arc" by the migrate_v7 pipeline. See
        # docs/spec-manifest-v7-2026-05-20.md.
        "manifest_shape": MANIFEST_SHAPE_LEGACY,
        # v15: per-app data classification for cloud backup. All three are
        # empty/absent by default — that means "no classification declared,"
        # which the resolver treats as "everything cloud-eligible" (pre-v15
        # behaviour, so existing manifests keep backing up exactly as
        # before). Spec: docs/spec-backup-and-data-classification-2026-05-28.md.
        "app_files_privacy": "",
        "data_paths": [],
        "default_for_unclassified": "",
        # v25: purpose/fit classification. Inert default — an un-classified
        # manifest is an "application" (stays on the page), and the audit
        # block is empty until the scanner's classifier judges it. See the
        # Schema v25 dataclass block + purpose_classifier.py.
        "app_kind": APP_KIND_APPLICATION,
        "classification": {},
        # v27: definition_status (Defined/Discovered source-of-truth axis).
        # DELIBERATE migration policy (§9.1): EVERY pre-existing manifest lands
        # at "discovered" regardless of its `source` — no bulk auto-promote.
        # An operator promotes the ones that should become the source of truth.
        # NEW authored creations (forge/gallery/user) stamp "defined" at their
        # creation site BEFORE first write, so this populate-on-absent default
        # never overrides a born-defined manifest. See born_definition_status().
        "definition_status": MANIFEST_DEFINITION_DISCOVERED,
        # v28: drift_log (drift-narrative log) — Bite 3 (spec §9.3). Append-only
        # MAJOR-drift narrative the scanner appends for ``defined`` apps during
        # the Phase-6 reconcile pass. Inert default [] — existing manifests
        # migrate with no narrative ("no major drift observed"). NOT change_log:
        # see the Schema v28 dataclass block for the collision resolution.
        "drift_log": [],
        # v16: forge side-effects install metadata lives *inside*
        # scheduled_actions[] entries (mechanism/install/installed_*). No
        # new top-level keys; the per-entry migration runs after the
        # defaults loop below. See _migrate_scheduled_action_entry below
        # and docs/spec-forge-side-effects-2026-06-02.md §4.
        #
        # v20: coherence + reconciliation framework. Adds four top-level
        # blocks, each safe-empty by default. The migration is purely
        # additive — populate-on-absent only, never overwrite. Spec:
        # docs/spec-app-coherence-and-reconciliation-2026-06-05.md.
        #
        #   provenance: who wrote what. manifest_origin and field_origins
        #     drive the reconciliation rules — observational fields update
        #     silently, authored fields stage chips. Populated by
        #     _populate_provenance_v20() below.
        #
        #   reconciliation: staging area for chips. status="ok" means no
        #     pending drift. Populated by the scanner/auditor at runtime;
        #     migration just creates the empty shell.
        #
        #   coherence: claim-vs-mechanism findings. status="ok" means no
        #     pending findings. coherence_accepted tracks signatures the
        #     operator has explicitly accepted.
        #
        #   volatile_paths: declared-volatile directory globs (data/, log/,
        #     content/, etc.). When populated, reconciliation skips per-file
        #     enumeration inside the glob.
        "provenance": {},  # populated by _populate_provenance_v20 below
        "reconciliation": {
            "last_reconciled_at": None,
            "status": "ok",
            "extra_files": [],
            "missing_files": [],
            "missing_crons": [],
            "missing_actions": [],
            "volatile_growth_anomalies": [],
            "operator_decisions": [],
        },
        "coherence": {
            "last_checked_at": None,
            "status": "ok",
            "findings": [],
            "last_capability_check": None,
            "coherence_accepted": [],
        },
        "volatile_paths": [],
        # v24: privacy + audience_scoping. Empty dict = "not yet declared" —
        # deliberately NOT the inferred defaults the v7 migration / forge
        # seeding write, so a migrated legacy manifest doesn't silently
        # acquire a declared trust boundary nobody authored. Spec:
        # docs/spec-manifest-v7-slicing-2026-06-10.md §4.3.
        "privacy": {},
        "audience_scoping": {},
    }

    for key, default in defaults.items():
        if key not in data:
            data[key] = default
            changed = True

    # v16: backfill install-metadata sub-fields on each scheduled_actions[]
    # entry. Pre-v16 entries lack `mechanism` and the `install` recipe;
    # mark them as `unknown` so downstream code can distinguish "scanner
    # hasn't attributed this yet" from genuine `external` or installed
    # mechanisms. Scanner attribution (Part B of the spec) will rewrite
    # `mechanism` for real on the next scan.
    #
    # v17: rewrite deprecated mechanism values (oc_heartbeat_hook →
    # oc_heartbeat_instruction; oc_session_hook → oc_session_instruction)
    # and clear the bogus installed_artifact so the next forge run
    # materializes to the correct surface (HEARTBEAT.md, not openclaw.json).
    # Spec: docs/spec-heartbeat-instruction-2026-06-03.md §9.
    sa_list = data.get("scheduled_actions")
    if isinstance(sa_list, list):
        for entry in sa_list:
            if not isinstance(entry, dict):
                continue
            if _migrate_scheduled_action_entry_v16(entry):
                changed = True
            if _migrate_scheduled_action_entry_v17(entry):
                changed = True
            # v20: state, quality, safety_net_for.
            # Must run after v16 (which populates `mechanism`) so the
            # quality assessment can read mechanism correctly.
            if _migrate_scheduled_action_entry_v20(entry):
                changed = True

    # v20: layer rename on each files[*] entry. The legacy stamper used
    # "script" where the v20 enum uses "code"; mismatched files still get
    # the right severity treatment under the new rules. PR 3's classifier
    # handles other mis-classifications (HEARTBEAT.md → behavior_doc,
    # content files → content, etc.) by re-classifying from scratch.
    files_list = data.get("files")
    if isinstance(files_list, list):
        for entry in files_list:
            if not isinstance(entry, dict):
                continue
            if _migrate_file_entry_v20(entry):
                changed = True

    # v20: provenance population is deferred to AFTER all other
    # migrations in migrate_manifest() — see the call site below the
    # evidence_files migration. Doing it here would miss fields added by
    # later legacy migrations (evidence_files, promoted crons, backfilled
    # capability_tags) and the second call would stamp them, breaking
    # idempotency.

    # Self-heal a corrupt manifest_shape discriminator. The defaults loop
    # above only *adds* manifest_shape when absent; it never corrects a
    # present-but-invalid value (e.g. "v20" — a schema-version string that
    # leaked into the field). Left uncorrected, such a value orphans the
    # manifest from both the promotion path and every v7/Reflect reader.
    # Runs before the provenance population below so field_origins captures
    # the corrected value.
    if _normalize_manifest_shape(data):
        changed = True

    # v27: coerce a corrupt definition_status back to the safe default. The
    # defaults loop above only ADDS the field when absent; a present-but-
    # out-of-enum value (an LLM/hand-edit leak) would otherwise read as
    # "discovered" everywhere by safe-degradation but stay non-conformant on
    # disk. Normalize it so the stored value always matches the closed enum.
    ds = data.get("definition_status")
    if ds is not None and ds not in VALID_MANIFEST_DEFINITION_STATUSES:
        data["definition_status"] = MANIFEST_DEFINITION_DISCOVERED
        changed = True

    # Bump the stamped schema_version to the current one when behind. This
    # is a metadata-only bump — the field-add loop above is what actually
    # brings the manifest's shape forward. Without this, a v11 file would
    # keep its "schema_version: 11" stamp even after gaining v12/v13 fields,
    # which is confusing in logs and breaks UI re-scan-strip detection.
    try:
        stored = int(data.get("schema_version") or 0)
    except (TypeError, ValueError):
        stored = 0
    if stored < MANIFEST_SCHEMA_VERSION:
        data["schema_version"] = MANIFEST_SCHEMA_VERSION
        changed = True

    # v7 migration: backfill capability_tags / session_keywords from display name
    # so the app-session correlator can attribute sessions to all existing manifests.
    if not data.get("capability_tags") or not data.get("session_keywords"):
        import re as _re
        _raw_name = (data.get("display_name") or data.get("name") or "").strip()
        if _raw_name:
            _tokens = [t for t in _re.split(r"[\s_\-]+", _raw_name.lower()) if len(t) > 2]
            if not data.get("capability_tags"):
                data["capability_tags"] = list(dict.fromkeys([_raw_name] + _tokens))
                changed = True
            if not data.get("session_keywords"):
                data["session_keywords"] = list(dict.fromkeys([_raw_name.lower()] + _tokens))
                changed = True

    # Promote raw cron strings to dicts and assign missing file_ids
    raw_crons = data.get("crons") or []
    if raw_crons:
        promoted = []
        crons_changed = False
        for c in raw_crons:
            if isinstance(c, str):
                d = _parse_cron_string(c)
                d["file_id"] = _new_file_id_import()
                promoted.append(d)
                crons_changed = True
            elif isinstance(c, dict):
                if not c.get("file_id"):
                    c = dict(c)
                    c["file_id"] = _new_file_id_import()
                    crons_changed = True
                # Ensure all expected keys present
                if "label" not in c or "script" not in c:
                    c = _promote_cron(c)
                    crons_changed = True
                promoted.append(c)
            else:
                promoted.append(c)
        if crons_changed:
            data["crons"] = promoted
            changed = True

    # Migrate evidence dict → evidence_files list if needed
    if "evidence_files" not in data:
        ev = data.get("evidence", {})
        if isinstance(ev, dict):
            files = ev.get("files", [])
        elif isinstance(ev, list):
            files = ev
        else:
            files = []
        data["evidence_files"] = files
        changed = True

    # v20: populate provenance block. Must run LAST after every other
    # migration so field_origins captures the complete set of top-level
    # fields. Running earlier would miss fields added by legacy
    # migrations (evidence_files, promoted crons, backfilled
    # capability_tags) and the second call would stamp them, breaking
    # idempotency. Per §13.1: every existing field defaults to
    # observational; forge-built manifests get forge_built.
    if _populate_provenance_v20(data):
        changed = True

    if changed:
        # Route through _write_manifest_bytes so the sudo-cp fallback kicks
        # in when path.parent is bot-owned and the evolve write ACL isn't
        # set yet. Without this, schema-migration writes silently fail and
        # callers like list_manifests swallow the PermissionError, dropping
        # the manifest from their results.
        try:
            _write_manifest_bytes(path, json.dumps(data, indent=2).encode("utf-8"))
        except Exception:
            # Migration is best-effort. If we can't write the bumped version,
            # the in-memory result returned to the caller is still good for
            # this read; the next writer can re-migrate then.
            pass
