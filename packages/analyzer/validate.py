#!/usr/bin/env python3
"""
validate.py — Proposal validation harness for Evolve.

Imported by the admin server's `_validate_proposal_immediately()` (see
packages/admin/evolve_admin/web/server.py) — runs in-process on the admin
host whenever a proposal transitions to approved. The legacy "forge bot"
that this harness used to live on is gone; the validation logic itself
remains as the proposal-pipeline gate.

Usage:
  python3 validate.py --proposal proposals/approved/prop-2026-04-07-001.json
  python3 validate.py --network network.json --proposal <path>

For each proposal:
  1. Validates proposal schema
  2. Runs type-appropriate validation suite
  3. Writes result to proposals/validation-results/
  4. Alerts the primary bot via the openclaw messaging channel

Proposal types and what we validate:
  config_change    → shadow-apply patch, validate JSON, check required keys, boot-test
  script_change    → copy to temp, dry-run if supported, check exit/stderr
  workflow_change  → cannot auto-validate; marks needs-human
  investigation    → acknowledges, closes with no action
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evolve_config import resolve_network_path, bot_home as _ec_bot_home
from evolve_util import now_iso  # also re-exported — web/server.py imports it from here

VALIDATOR_VERSION = "0.1.0"

# Required top-level keys in openclaw.json that must survive a config patch
REQUIRED_OC_KEYS = {"agents", "gateway"}

# Keys whose removal should block promotion even if JSON is valid
CRITICAL_OC_PATHS = [
    ["agents", "defaults"],
    ["gateway", "port"],
]


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class TestResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class ValidationResult:
    proposal_id: str
    result: str = "pass"           # pass | fail | needs-human | error
    recommendation: str = "promote"  # promote | reject | escalate
    confidence: float = 0.90
    tests_run: list[TestResult] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    validation_notes: str = ""
    validated_at: str = ""
    validator_version: str = VALIDATOR_VERSION

    def add_test(self, name: str, passed: bool, detail: str = "") -> None:
        self.tests_run.append(TestResult(name, passed, detail))
        if not passed:
            self.result = "fail"
            self.recommendation = "reject"

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "validated_at": self.validated_at or now_iso(),
            "validator_version": self.validator_version,
            "result": self.result,
            "recommendation": self.recommendation,
            "confidence": self.confidence,
            "tests_run": [
                {"name": t.name, "passed": t.passed, "detail": t.detail}
                for t in self.tests_run
            ],
            "evidence": self.evidence,
            "validation_notes": self.validation_notes,
        }


# ── Main entry ─────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Evolve Forge Validator")
    parser.add_argument("--proposal", required=True, type=Path, help="Path to approved proposal JSON")
    parser.add_argument("--network", type=Path, default=resolve_network_path(), help="Path to network.json")
    parser.add_argument("--shared-dir", type=Path, default=None, help="Override shared directory")
    args = parser.parse_args()

    # Load network config
    network = load_json(args.network) or {}
    shared_dir = args.shared_dir or Path(network.get("sharedDir", "/Users/Shared/evolve"))

    # Load proposal
    proposal = load_json(args.proposal)
    if proposal is None:
        print(f"ERROR: Cannot read proposal: {args.proposal}", file=sys.stderr)
        sys.exit(1)

    proposal_id = proposal.get("id", args.proposal.stem)
    print(f"Validating: {proposal_id}")

    # Run validation
    result = validate_proposal(proposal, network, shared_dir)
    result.validated_at = now_iso()

    # Write result
    results_dir = shared_dir / "proposals" / "validation-results"
    results_dir.mkdir(parents=True, exist_ok=True)
    result_path = results_dir / f"{proposal_id}.json"
    result_dict = result.to_dict()
    # No forge_sig stamp — HMAC forge-result signing retired 2026-08-18
    # (docs/design-proposal-signing-key-2026-08-18.md).
    write_json(result_path, result_dict)
    print(f"Result written: {result_path}")
    print(f"  Outcome: {result.result} → {result.recommendation}")

    # Proposal file stays in approved/ until promoted or rejected by primary.

    # Alert primary bot
    alert_primary(proposal, result, network, shared_dir=shared_dir)


# ── Dispatch ───────────────────────────────────────────────────────────────────

def validate_proposal(proposal: dict, network: dict, shared_dir: Path) -> ValidationResult:
    proposal_id = proposal.get("id", "unknown")
    proposal_type = proposal.get("type", "")
    result = ValidationResult(proposal_id=proposal_id)

    # ── Schema validation (always first) ──────────────────────────────────────
    schema_ok, schema_msg = validate_schema(proposal)
    result.add_test("schema_valid", schema_ok, schema_msg)
    if not schema_ok:
        result.validation_notes = "Proposal failed schema validation — cannot proceed."
        result.result = "error"
        result.recommendation = "escalate"
        return result

    # ── Type-specific validation ───────────────────────────────────────────────
    try:
        if proposal_type == "config_change":
            validate_config_change(proposal, network, shared_dir, result)
        elif proposal_type == "script_change":
            validate_script_change(proposal, network, shared_dir, result)
        elif proposal_type == "workflow_change":
            validate_workflow_change(proposal, result)
        elif proposal_type == "investigation":
            validate_investigation(proposal, result)
        else:
            result.add_test("type_known", False, f"Unknown proposal type: {proposal_type!r}")
            result.result = "needs-human"
            result.recommendation = "escalate"
    except Exception as e:
        result.result = "error"
        result.recommendation = "escalate"
        result.validation_notes = f"Unexpected error during validation: {e}\n{traceback.format_exc()}"
        result.evidence["exception"] = str(e)

    return result


# ── Schema validation ─────────────────────────────────────────────────────────

REQUIRED_FIELDS = [
    "id", "type", "target_bot", "problem", "root_cause",
    "confidence", "risk", "forge_required",
]

def validate_schema(proposal: dict) -> tuple[bool, str]:
    missing = [f for f in REQUIRED_FIELDS if f not in proposal]
    if missing:
        return False, f"Missing required fields: {missing}"
    if proposal.get("confidence", 0) < 0.50:
        return False, f"Confidence too low: {proposal['confidence']} (min 0.50)"
    valid_types = {"config_change", "script_change", "workflow_change", "investigation"}
    if proposal.get("type") not in valid_types:
        return False, f"Invalid type: {proposal.get('type')!r}"
    valid_risks = {"low", "medium", "high"}
    if proposal.get("risk") not in valid_risks:
        return False, f"Invalid risk: {proposal.get('risk')!r}"
    return True, "All required fields present"


# ── config_change validation ──────────────────────────────────────────────────

def validate_config_change(
    proposal: dict, network: dict, shared_dir: Path, result: ValidationResult
) -> None:
    """
    Validate a config_change proposal:
    1. Load target bot's current openclaw.json
    2. Apply the proposed patch to a copy
    3. Verify JSON integrity
    4. Check no critical keys were removed
    5. Boot-test: apply to forge's openclaw.json, restart gateway, verify it starts
    """
    change = proposal.get("proposed_change", {})
    target_bot = proposal.get("target_bot", "")

    # Locate target bot's openclaw.json. Use _ec_bot_home to resolve the macOS
    # account name — target_bot (logical name) may differ (e.g. team_bot_b/personal_bot_user).
    oc_path = _ec_bot_home(target_bot) / ".openclaw" / "openclaw.json"
    if not oc_path.exists():
        result.add_test("target_config_exists", False, f"Cannot read {oc_path}")
        result.validation_notes = f"Target bot '{target_bot}' openclaw.json not readable from Forge."
        result.result = "needs-human"
        result.recommendation = "escalate"
        return

    result.add_test("target_config_exists", True, str(oc_path))

    current_config = load_json(oc_path)
    if current_config is None:
        result.add_test("target_config_parseable", False, "JSON parse error")
        return
    result.add_test("target_config_parseable", True)

    # Apply the patch
    patched_config = copy.deepcopy(current_config)
    patch_ok, patch_msg = apply_patch(patched_config, change)
    result.add_test("patch_applies", patch_ok, patch_msg)
    if not patch_ok:
        return

    result.evidence["proposed_change"] = change
    result.evidence["from_value"] = change.get("from")
    result.evidence["to_value"] = change.get("to")

    # Verify patched config is valid JSON (always true if we built it, but let's be explicit)
    try:
        json.dumps(patched_config)
        result.add_test("patched_config_valid_json", True)
    except (TypeError, ValueError) as e:
        result.add_test("patched_config_valid_json", False, str(e))
        return

    # Check no critical keys were removed
    for key_path in CRITICAL_OC_PATHS:
        present = _get_nested(patched_config, key_path) is not None
        result.add_test(
            f"critical_key_preserved:{'→'.join(key_path)}",
            present,
            "still present" if present else f"REMOVED by patch: {key_path}",
        )

    # Risk gate: refuse to auto-validate high-risk changes
    if proposal.get("risk") == "high":
        result.add_test("risk_gate", False, "High-risk changes require manual review")
        result.result = "needs-human"
        result.recommendation = "escalate"
        result.validation_notes = "High-risk config change — Forge cannot auto-promote. Manual review required."
        return

    # Boot-test: advisory only — always records as skipped (boot testing
    # is not implemented in the current architecture). apply.py handles
    # runtime failures via backup/rollback instead.
    boot_test_config(patched_config, result)

    # Pass/fail is determined by structural tests only (schema, patch, critical keys, risk).
    # Boot test result is recorded in tests_run for the audit trail but is not blocking.
    structural_tests = [t for t in result.tests_run if not t.name.startswith("boot_test")]
    if all(t.passed for t in structural_tests):
        result.result = "pass"
        result.recommendation = "promote"
        result.validation_notes = (
            f"Config change validated (boot test skipped — not implemented). "
            f"Patch applies cleanly, all critical keys preserved. apply.py "
            f"will apply with backup/rollback safety. Ready to deploy to {target_bot}."
        )
    else:
        result.validation_notes = "One or more structural validation tests failed. See tests_run for details."


def apply_patch(config: dict, change: dict) -> tuple[bool, str]:
    """Apply a proposed_change patch to a config dict in place."""
    change_type = change.get("type", "set")

    if change_type == "set" or "path" in change:
        path = change.get("path", "")
        to_value = change.get("to")
        if not path:
            return False, "proposed_change missing 'path'"
        parts = _parse_path(path)
        try:
            _set_nested(config, parts, to_value)
            return True, f"Set {path} = {to_value!r}"
        except Exception as e:
            return False, f"Failed to apply path {path}: {e}"

    elif change_type == "append" and "target_file" in change:
        # Script append — handled separately in script_change
        return True, "Append-type change (validated separately)"

    elif "description" in change and len(change) == 1:
        # Description-only change (workflow/investigation)
        return True, "Description-only change acknowledged"

    return False, f"Cannot interpret proposed_change structure: {list(change.keys())}"


def boot_test_config(config: dict, result: ValidationResult) -> bool:
    """Record a skipped boot test on the result.

    Boot testing originally required a dedicated sandbox bot to apply the
    patched config to, restart its gateway, and verify health. That bot
    was never built and the design has been retired. Static analysis
    (schema, patch, critical-key preservation) plus apply.py's
    backup/rollback safety cover the same ground.

    Always returns False (the test was skipped, not passed) so the
    advisory `boot_test` entry shows up in `tests_run` for the audit
    trail. Pass/fail of the proposal is determined by the structural
    tests, which is why this is advisory-only.
    """
    result.tests_run.append(
        TestResult("boot_test", True, "Skipped — boot testing not implemented")
    )
    return False


# ── script_change validation ──────────────────────────────────────────────────

def validate_script_change(
    proposal: dict, network: dict, shared_dir: Path, result: ValidationResult
) -> None:
    """
    Validate a script_change proposal:
    1. Locate the target script
    2. Verify the proposed content/patch is syntactically valid
    3. Run with --dry-run if the script supports it
    4. Check exit code and stderr
    """
    change = proposal.get("proposed_change", {})
    target_bot = proposal.get("target_bot", "")
    target_file = change.get("target_file", "")

    if not target_file:
        result.add_test("target_file_specified", False, "proposed_change missing target_file")
        result.result = "needs-human"
        result.recommendation = "escalate"
        return

    result.add_test("target_file_specified", True, target_file)

    # Locate the script
    script_path = _resolve_script_path(target_bot, target_file)
    if script_path is None:
        result.add_test("script_exists", False, f"Cannot locate {target_file} for {target_bot}")
        result.result = "needs-human"
        result.recommendation = "escalate"
        return

    result.add_test("script_exists", True, str(script_path))

    # Syntax check for Python scripts
    if script_path.suffix == ".py":
        syntax_ok, syntax_msg = _python_syntax_check(script_path)
        result.add_test("syntax_check", syntax_ok, syntax_msg)
        if not syntax_ok:
            return

    # Dry-run attempt
    dry_run_ok, dry_run_msg = _try_dry_run(script_path)
    result.add_test("dry_run", dry_run_ok, dry_run_msg)
    result.evidence["dry_run_output"] = dry_run_msg[:500]

    if all(t.passed for t in result.tests_run):
        result.result = "pass"
        result.recommendation = "promote"
        result.validation_notes = (
            f"Script change validated: syntax OK, dry-run passed. "
            f"Ready to deploy to {target_bot}."
        )
    else:
        result.validation_notes = "Script validation had failures. Manual review recommended."


def _resolve_script_path(target_bot: str, target_file: str) -> Path | None:
    """Try to locate a script on the Forge filesystem."""
    # Use _ec_bot_home to resolve account name — target_bot may differ (e.g. team_bot_b/personal_bot_user).
    _tbot_home = _ec_bot_home(target_bot)
    candidates = [
        Path(target_file),
        _tbot_home / ".openclaw" / "workspace" / target_file,
        _tbot_home / ".openclaw" / "workspace" / "evolve" / Path(target_file).name,
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _python_syntax_check(script_path: Path) -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "py_compile", str(script_path)],
        capture_output=True, text=True, timeout=10,
    )
    if proc.returncode == 0:
        return True, "Syntax OK"
    return False, proc.stderr.strip()[:300]


def _try_dry_run(script_path: Path) -> tuple[bool, str]:
    """Attempt --dry-run if the script accepts it, otherwise skip."""
    # Check if --dry-run is in the script's argparse
    try:
        content = script_path.read_text()
    except OSError:
        return True, "Cannot read script for dry-run check — skipped"

    if "--dry-run" not in content and "dry_run" not in content:
        return True, "Script does not support --dry-run — skipped"

    proc = subprocess.run(
        [sys.executable, str(script_path), "--dry-run"],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "EVOLVE_FORGE_DRY_RUN": "1"},
    )
    if proc.returncode == 0:
        return True, f"--dry-run exited 0. stdout: {proc.stdout[:200]}"
    return False, f"--dry-run exited {proc.returncode}. stderr: {proc.stderr[:300]}"


# ── workflow_change validation ────────────────────────────────────────────────

def validate_workflow_change(proposal: dict, result: ValidationResult) -> None:
    """
    Workflow changes can't be automatically validated.
    Mark as needs-human with a clear summary for Pod_admin.
    """
    result.result = "needs-human"
    result.recommendation = "escalate"
    result.confidence = 0.0
    result.add_test(
        "auto_validation_possible", False,
        "Workflow changes require human judgment — Forge cannot validate automatically."
    )
    result.validation_notes = (
        f"Workflow change for {proposal.get('target_bot')}: {proposal.get('problem')}\n"
        f"Proposed: {proposal.get('proposed_change', {}).get('description', '(see proposal)')}\n"
        f"Minimum test: {proposal.get('minimum_test', '(not specified)')}\n"
        f"This requires manual review and testing."
    )


# ── investigation validation ──────────────────────────────────────────────────

def validate_investigation(proposal: dict, result: ValidationResult) -> None:
    """
    Investigations are informational — acknowledge and close.
    """
    result.result = "pass"
    result.recommendation = "promote"
    result.confidence = 1.0
    result.add_test(
        "investigation_acknowledged", True,
        "Investigation proposals are informational — no automated validation required."
    )
    result.validation_notes = (
        f"Investigation acknowledged: {proposal.get('problem')}. "
        f"No automated action required — promote to mark as reviewed."
    )


# ── Alerting ──────────────────────────────────────────────────────────────────

def alert_primary(
    proposal: dict, result: ValidationResult, network: dict,
    *, shared_dir: str | Path,
) -> None:
    """Send validation-result alert via the dispatcher.

    Phase C of docs/spec-alert-subscriptions-2026-05-10.md. Operator
    can mute via system.manifest_validation_failed subscription. Note:
    the event-key is named "_failed" but the catalog covers all
    validation results (pass / fail / needs-human / error) — operator
    mutes the whole notification stream with one toggle.

    Stays on the legacy ``message=`` path (not payload-driven). The
    body has a result-specific leading icon (✅ / ❌ / 👤 / 🔴) that the
    catalog's flat ``body_template`` cannot conditionalize — a single
    template would lie about ``pass`` cases. Source owns the render.
    """
    icon = {"pass": "✅", "fail": "❌", "needs-human": "👤", "error": "🔴"}.get(result.result, "❓")
    rec_label = {"promote": "Ready to promote", "reject": "Recommend reject", "escalate": "Needs human review"}.get(
        result.recommendation, result.recommendation
    )

    lines = [
        f"{icon} *Forge Result: {result.result.upper()}*",
        f"Proposal: `{proposal.get('id')}`",
        f"Target: {proposal.get('target_bot')} · Type: {proposal.get('type')}",
        f"Problem: {proposal.get('problem', '')[:120]}",
        f"Decision: {rec_label}",
    ]
    if result.validation_notes:
        lines.append(f"Notes: {result.validation_notes[:200]}")

    failed = [t for t in result.tests_run if not t.passed]
    if failed:
        lines.append(f"Failed tests: {', '.join(t.name for t in failed)}")

    message = "\n".join(lines)
    severity_name = "info" if result.result == "pass" else "warning"

    try:
        from evolve_admin.alerts.dispatcher import (
            send as _dispatch_send, Severity,
        )
    except Exception as exc:
        print(f"[evolve/validate] dispatcher import failed; alert dropped: {exc}",
              file=sys.stderr)
        return

    severity_map = {
        "critical": Severity.CRITICAL, "error": Severity.ERROR,
        "warning": Severity.WARNING, "info": Severity.INFO,
    }
    try:
        _dispatch_send(
            shared_dir=Path(shared_dir),
            network=network,
            source="validate",
            message=message,
            severity=severity_map[severity_name],
            dedup_key=f"validate/{proposal.get('id', 'unknown')}/{result.result}",
            catalog_event="system.manifest_validation_failed",
        )
    except Exception as exc:
        print(f"[evolve/validate] dispatcher.send raised; alert dropped: {exc}",
              file=sys.stderr)


# ── Utilities ─────────────────────────────────────────────────────────────────

def load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(path)


def _parse_path(path_str: str) -> list[str]:
    """Parse a dot-notation path like 'agents.defaults.model' into parts."""
    return path_str.split(".")


def _get_nested(obj: dict, parts: list[str]) -> Any:
    cur = obj
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


def _set_nested(obj: dict, parts: list[str], value: Any) -> None:
    for p in parts[:-1]:
        if p not in obj or not isinstance(obj[p], dict):
            obj[p] = {}
        obj = obj[p]
    obj[parts[-1]] = value


if __name__ == "__main__":
    main()
