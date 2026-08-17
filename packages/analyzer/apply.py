#!/usr/bin/env python3
"""
apply.py — Per-bot self-application watcher.

Runs on EACH bot (not just primary) as that bot's own user.
Polls proposals/approved/ for proposals targeting this bot,
applies them within this bot's own user context (no sudo needed),
and writes results back to the shared directory.

This is the key to autonomous pod-wide adaptation: each bot applies
its own changes. The proposer and applier may be different bots.
No cross-user writes. No human admin needed for execution of approved
proposals.

Usage:
  python3 apply.py --bot-id <bot_id> --network network.json
  python3 apply.py --bot-id team_bot_a --network network.json --dry-run

Scheduled: launchd StartInterval=300 (every 5 minutes), per bot.

What it can apply:
  config_change   → patches this bot's openclaw.json (restarts gateway after)
  script_change   → writes/patches a script in this bot's workspace
  workflow_change → writes a markdown instruction file for the bot to read
  investigation   → acknowledges, closes, no action

Safety gates:
  - Only applies proposals targeting THIS bot (bot_id match)
  - Only applies proposals with status=approved AND a passing forge result
  - High-risk proposals require forge result to exist (can't self-apply blindly)
  - Config changes are backed up before patching
  - Gateway restart is attempted; if it fails, the backup is restored
  - All results reported back to shared dir + Telegram alert
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from evolve_config import CANONICAL_SHARED_DIR, resolve_network_path, verify_proposal_sig, verify_review_stamp, verify_forge_result_sig, bot_home as _bot_home
from evolve_util import now_iso

# Tool-suspend guard — see SKILL spec §5.5.5c. If apply.py's meta-test
# (M2 in tool-meta-tests catalog) detects that apply produces wrong
# states, the worker writes a suspend flag and apply must stop acting
# until the meta-test goes green again.
try:
    from evolve_admin.tool_suspend import guard as _suspend_guard
except ImportError:
    def _suspend_guard(_tool: str, log_fn=None, repo_root=None) -> bool:
        return False

APPLIER_VERSION = "0.1.0"
PROCESSED_REGISTRY = "apply_processed.json"


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class ApplyResult:
    proposal_id: str
    bot_id: str
    success: bool
    action_taken: str = ""
    details: str = ""
    rollback_triggered: bool = False
    applied_at: str = ""
    applier_version: str = APPLIER_VERSION

    def to_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "bot_id": self.bot_id,
            "success": self.success,
            "action_taken": self.action_taken,
            "details": self.details,
            "rollback_triggered": self.rollback_triggered,
            "applied_at": self.applied_at or now_iso(),
            "applier_version": self.applier_version,
        }


# ── Main entry ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Evolve per-bot apply watcher")
    parser.add_argument("--bot-id", required=True, help="This bot's ID")
    parser.add_argument("--network", type=Path, default=resolve_network_path(), help="Path to network.json")
    parser.add_argument("--shared-dir", type=Path, default=None, help="Override shared directory")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be applied, don't apply")
    parser.add_argument("--daemon", action="store_true", help="Run continuously (for testing)")
    parser.add_argument("--interval", type=int, default=300, help="Poll interval in seconds")
    args = parser.parse_args()

    # Resolve shared dir from network.json or default
    shared_dir = _resolve_shared_dir(args.network, args.shared_dir)

    # Load config for module check
    from evolve_config import load_config as _lc, is_module_enabled, is_rsi_enabled
    _cfg = _lc(str(args.network) if args.network else None)
    if not is_rsi_enabled(_cfg):
        print("[apply] RSI feedback loop is disabled — exiting.")
        import sys; sys.exit(0)
    if not is_module_enabled(_cfg, "apply"):
        print("[apply] apply module is disabled — exiting.")
        import sys; sys.exit(0)

    print(f"[{now()}] apply.py starting — bot: {args.bot_id}, shared: {shared_dir}")

    if args.daemon:
        while True:
            run_once(args.bot_id, shared_dir, args.dry_run)
            time.sleep(args.interval)
    else:
        run_once(args.bot_id, shared_dir, args.dry_run)


# Top-level keys that must be present in openclaw.json after any patch.
REQUIRED_OC_KEYS = {"agents", "gateway"}

# Nested paths that must survive any patch (dot-notation).
# These are structural minimums — removing them makes the gateway unbootable.
CRITICAL_OC_PATHS = [
    "agents.defaults",
    "gateway.port",
]

# Config paths that must never be modified via autonomous proposals.
# These control network binding, authentication, and process identity.
CONFIG_PATH_BLOCKLIST = {
    "gateway.bind",
    "gateway.port",
    "gateway.remote",
    "gateway.token",
    "gateway.secret",
    "auth",
    "auth.token",
    "auth.apiKey",
    "channel",           # channel config (Telegram tokens etc.)
    "plugins",           # plugin activation changes are higher-risk
}

# Backup retention: delete backups older than this many seconds
BACKUP_MAX_AGE_SECS = 86400  # 24 hours — give operators a full day to catch config bugs


def _cleanup_old_backups(bot_id: str) -> None:
    """Delete openclaw.json backup files older than BACKUP_MAX_AGE_SECS."""
    import time as _time
    oc_dir = _bot_home(bot_id) / ".openclaw"
    if not oc_dir.exists():
        return
    now_ts = _time.time()
    for bak in oc_dir.glob("openclaw.json.bak.*"):
        try:
            # Timestamp is encoded in the suffix after the last dot
            ts = float(bak.suffix.lstrip("."))
            if now_ts - ts > BACKUP_MAX_AGE_SECS:
                bak.unlink(missing_ok=True)
                print(f"[apply] Cleaned up old backup: {bak.name}")
        except (ValueError, OSError):
            pass


def run_once(bot_id: str, shared_dir: Path, dry_run: bool) -> None:
    # Tool-suspend guard — runs first, before any proposal application.
    # If the M2 meta-test caught apply.py reporting wrong outcomes
    # (e.g. saying "applied" when the state didn't actually change),
    # the worker has set the suspend flag. We MUST skip this cycle to
    # avoid further harm. The flag is cleared automatically by the
    # worker's step 5a re-verify when M2 goes green again.
    if _suspend_guard("apply", log_fn=lambda m: print(f"[evolve/apply] {m}")):
        return

    # Clean up old config backups from previous runs
    _cleanup_old_backups(bot_id)

    approved_dir = shared_dir / "proposals" / "approved"
    results_dir = shared_dir / "proposals" / "apply-results"
    validation_results_dir = shared_dir / "proposals" / "validation-results"
    legacy_validation_results_dir = shared_dir / "proposals" / "forge-results"
    quarantine_dir = shared_dir / "proposals" / "quarantine"
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    if not approved_dir.exists():
        print(f"[{now()}] No approved/ directory yet — nothing to do.")
        return

    # Load processed registry — hold an exclusive lock for the entire cycle to
    # prevent two concurrent apply.py instances (e.g. manual trigger + scheduled)
    # from processing the same proposal twice.
    registry_path = shared_dir / PROCESSED_REGISTRY.replace(".json", f"-{bot_id}.json")
    lock_path = registry_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = open(lock_path, "w")
    fcntl.flock(lock_fh, fcntl.LOCK_EX)
    try:
        processed: set[str] = set(load_json(registry_path) or [])

        # Find proposals targeting this bot that haven't been processed
        mine = [
            f for f in sorted(approved_dir.iterdir())
            if f.suffix == ".json"
            and f.stem not in processed
            and _targets_bot(f, bot_id)
        ]

        if not mine:
            print(f"[{now()}] No new proposals for {bot_id}.")
            return

        print(f"[{now()}] Found {len(mine)} proposal(s) for {bot_id}.")
        results_dir.mkdir(parents=True, exist_ok=True)

        # Surface any proposal aging in approved/ — a stuck proposal here
        # was the root of the 2026-05-20 team_bot_a bleed (heal-team_bot_a-1776294604 sat
        # for ~35 days, repeatedly re-applied, $13 of silent Sonnet spend).
        # Track #3 owns the proper stuck_proposal Signal; this is the
        # stopgap so the next one shows up in apply log + launchd err.log.
        _STALE_DAYS = 7
        now_ts = time.time()
        for f in mine:
            try:
                age_days = (now_ts - f.stat().st_mtime) / 86400.0
            except OSError:
                continue
            if age_days > _STALE_DAYS:
                print(
                    f"[WARN] proposal {f.stem} has been in approved/ for "
                    f"{age_days:.1f} days. If apply.py keeps re-acting on "
                    f"the same id, it is stuck — investigate.",
                    file=sys.stderr,
                )

        for proposal_path in mine:
            proposal_id = proposal_path.stem
            print(f"[{now()}] Applying: {proposal_id}")

            # Idempotency: skip if already applied
            result_path = results_dir / f"{proposal_id}.json"
            if result_path.exists():
                print(f"  Already applied — skipping")
                processed.add(proposal_id)
                continue

            proposal = load_json(proposal_path)
            if not proposal:
                print(f"  Cannot read proposal — skipping")
                continue

            # Schema validation — catch malformed proposals before dispatch
            schema_error = _validate_proposal_schema(proposal)
            if schema_error:
                print(f"  Invalid proposal schema: {schema_error} — skipping")
                _write_result(results_dir, ApplyResult(
                    proposal_id=proposal_id, bot_id=bot_id, success=False,
                    action_taken="skipped",
                    details=f"Schema validation failed: {schema_error}",
                ))
                processed.add(proposal_id)
                continue

            # Signature verification — evolve_sig must be valid if a key exists
            if not verify_proposal_sig(proposal):
                print(f"  CRITICAL: {proposal_id} has invalid evolve_sig — quarantining, NOT applying")
                _quarantine_proposal(proposal, proposal_path, quarantine_dir, results_dir, bot_id,
                                     "invalid_evolve_sig")
                processed.add(proposal_id)
                continue

            # Review stamp verification — proposal must have passed review with valid sig
            if not verify_review_stamp(proposal):
                print(f"  CRITICAL: {proposal_id} has missing/invalid review_stamp.sig — quarantining")
                _quarantine_proposal(proposal, proposal_path, quarantine_dir, results_dir, bot_id,
                                     "invalid_review_stamp")
                processed.add(proposal_id)
                continue

            # Safety: require validation result for non-investigation proposals.
            # Read from the new dir first, fall back to the legacy dir for results
            # written before the rename migration ran on this pod.
            validation_result = (
                load_json(validation_results_dir / f"{proposal_id}.json")
                or load_json(legacy_validation_results_dir / f"{proposal_id}.json")
            )
            if proposal.get("type") not in ("investigation", "workflow_change"):
                if not validation_result:
                    print(f"  No validation result yet — skipping (will retry when validation completes)")
                    continue
                if not verify_forge_result_sig(validation_result):
                    print(f"  CRITICAL: {proposal_id} validation result has invalid sig — quarantining")
                    _quarantine_proposal(proposal, proposal_path, quarantine_dir, results_dir, bot_id,
                                         "invalid_validation_result_sig")
                    processed.add(proposal_id)
                    continue
                if validation_result.get("result") not in ("pass",) and validation_result.get("recommendation") != "promote":
                    print(f"  Validation result is '{validation_result.get('result')}' → not applying")
                    _write_result(results_dir, ApplyResult(
                        proposal_id=proposal_id, bot_id=bot_id, success=False,
                        action_taken="skipped",
                        details=f"Validation result '{validation_result.get('result')}' not eligible for auto-apply",
                    ))
                    processed.add(proposal_id)
                    continue

            if dry_run:
                print(f"  [dry-run] Would apply {proposal.get('type')}: {proposal.get('problem','')[:60]}")
                processed.add(proposal_id)
                continue

            # Apply
            result = apply_proposal(proposal, bot_id, shared_dir)
            result.applied_at = now_iso()
            _write_result(results_dir, result)
            processed.add(proposal_id)

            # Alert
            _alert(proposal, result, shared_dir)

            # Trigger regression tests if apply succeeded
            if result.success:
                _trigger_regression_tests(bot_id, shared_dir)
                _register_outcome(proposal, result, bot_id, shared_dir)

            status = "✓" if result.success else "✗"
            print(f"  {status} {result.action_taken}: {result.details[:80]}")

            # Save registry after each proposal so progress is preserved on crash
            save_json(registry_path, sorted(processed))
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


# ── Dispatch ──────────────────────────────────────────────────────────────────

def apply_proposal(proposal: dict, bot_id: str, shared_dir: Path) -> ApplyResult:
    proposal_type = proposal.get("type", "")
    result = ApplyResult(proposal_id=proposal.get("id", "?"), bot_id=bot_id, success=True)

    try:
        if proposal_type == "config_change":
            _apply_config_change(proposal, bot_id, result)
        elif proposal_type == "script_change":
            _apply_script_change(proposal, bot_id, result)
        elif proposal_type == "workflow_change":
            _apply_workflow_change(proposal, bot_id, shared_dir, result)
        elif proposal_type == "investigation":
            result.action_taken = "acknowledged"
            result.details = "Investigation noted. No automated action required."
        else:
            result.success = False
            result.action_taken = "unknown_type"
            result.details = f"Unknown proposal type: {proposal_type!r}"
    except Exception as e:
        result.success = False
        result.action_taken = "error"
        result.details = f"Unexpected error: {e}\n{traceback.format_exc()[:500]}"

    return result


# ── config_change ─────────────────────────────────────────────────────────────

def _apply_config_change(proposal: dict, bot_id: str, result: ApplyResult) -> None:
    """
    Apply a config_change to this bot's openclaw.json.
    1. Backup current config
    2. Apply patch
    3. Validate JSON
    4. Restart gateway
    5. Verify gateway responds
    6. Rollback if health check fails
    """
    oc_path = _bot_home(bot_id) / ".openclaw" / "openclaw.json"
    if not oc_path.exists():
        result.success = False
        result.action_taken = "config_not_found"
        result.details = f"Cannot find openclaw.json at {oc_path}"
        return

    change = proposal.get("proposed_change", {})
    current = load_json(oc_path)
    if current is None:
        result.success = False
        result.action_taken = "config_parse_error"
        result.details = "Cannot parse current openclaw.json"
        return

    # Backup
    backup_path = oc_path.with_suffix(f".json.bak.{int(time.time())}")
    shutil.copy2(oc_path, backup_path)

    # Apply patch
    patched = copy.deepcopy(current)
    path_str = change.get("path", "")
    to_value = change.get("to")
    from_value = change.get("from")

    if not path_str:
        result.success = False
        result.action_taken = "no_path"
        result.details = "proposed_change missing 'path'"
        return

    # Block modifications to sensitive config paths (gateway.bind, auth, etc.)
    # These require manual operator action, not autonomous apply.
    # Check the full path AND every dot-notation prefix against the blocklist.
    # e.g. "auth.token.value" checks "auth", "auth.token", "auth.token.value".
    # A simple root-only check would miss "auth_override.x" but also miss deeper
    # paths that should be blocked because an ancestor key is blocked.
    parts = path_str.split(".")
    path_prefixes = {".".join(parts[:i]) for i in range(1, len(parts) + 1)}
    if path_prefixes & CONFIG_PATH_BLOCKLIST:
        result.success = False
        result.action_taken = "path_blocked"
        result.details = (
            f"Config path '{path_str}' is in the autonomous-apply blocklist. "
            f"This path controls network binding, auth, or channels and requires "
            f"manual operator review. Apply this change manually via ocadmin."
        )
        return

    try:
        _set_nested(patched, parts, to_value)
    except Exception as e:
        result.success = False
        result.action_taken = "patch_failed"
        result.details = f"Could not apply patch to {path_str}: {e}"
        return

    # Validate critical structure was not destroyed by the patch
    missing_keys = [k for k in REQUIRED_OC_KEYS if k not in patched]
    if missing_keys:
        result.success = False
        result.action_taken = "patch_destroys_required_key"
        result.details = f"Patch would remove required top-level key(s): {missing_keys}. Refusing to apply."
        return

    for critical_path in CRITICAL_OC_PATHS:
        parts = critical_path.split(".")
        node = patched
        for part in parts:
            if not isinstance(node, dict) or part not in node:
                result.success = False
                result.action_taken = "patch_destroys_critical_path"
                result.details = f"Patch would remove critical config path '{critical_path}'. Refusing to apply."
                return
            node = node[part]

    # Write patched config
    try:
        _write_bot_config(oc_path, patched)
    except Exception as e:
        result.success = False
        result.action_taken = "write_failed"
        result.details = f"Could not write patched config: {e}"
        return

    port = patched.get("gateway", {}).get("port") or current.get("gateway", {}).get("port", 19000)

    # Restart gateway
    gateway_restarted = _restart_gateway(bot_id)

    if not gateway_restarted:
        # Gateway was not running and could not be started (e.g. fresh setup
        # before the system plist exists).  Config was written successfully —
        # skip the health check rather than rolling back a sound change.
        result.action_taken = "config_patched_no_gateway"
        result.details = (
            f"Set {path_str} = {to_value!r} (was {from_value!r}). "
            f"Gateway not running — skipped health check."
        )
        return

    # Poll for gateway health rather than a fixed sleep.
    # launchd may apply backoff on top of ThrottleInterval, so a fixed 6s
    # sleep is not reliable.  Poll for up to 30s before declaring failure.
    healthy = False
    for _ in range(10):
        time.sleep(3)
        if _check_health(port):
            healthy = True
            break

    if healthy:
        result.action_taken = "config_patched"
        result.details = f"Set {path_str} = {to_value!r} (was {from_value!r}). Gateway healthy."
        # Keep backup for 1 hour — don't delete immediately.
        # Delayed effect config bugs (gateway crashes 30s later) need a recovery path.
        # Backup cleanup happens in the next run_once() sweep via _cleanup_old_backups().
    else:
        # Rollback — only if backup actually exists (guard against fresh-setup edge case)
        if backup_path.exists():
            shutil.copy2(backup_path, oc_path)
            _restart_gateway(bot_id)
            time.sleep(6)
            rollback_healthy = _check_health(port)
        else:
            rollback_healthy = False
        result.success = False
        result.rollback_triggered = True
        if rollback_healthy:
            result.action_taken = "config_rolled_back"
            result.details = (
                f"Applied patch ({path_str} = {to_value!r}) but gateway health check failed. "
                f"Rolled back successfully — gateway healthy."
            )
        else:
            result.action_taken = "rollback_unhealthy"
            result.details = (
                f"Applied patch ({path_str} = {to_value!r}), health check failed, "
                f"rolled back, but post-rollback gateway is ALSO unhealthy. "
                f"Manual intervention required. Backup preserved at: {backup_path}"
            )
            # Don't delete backup — it's the only recovery path
            return


# ── script_change ─────────────────────────────────────────────────────────────

def _apply_script_change(proposal: dict, bot_id: str, result: ApplyResult) -> None:
    """
    Write or patch a script in this bot's workspace.
    """
    change = proposal.get("proposed_change", {})
    target_file = change.get("target_file", "")
    new_content = change.get("content", "")
    patch_type = change.get("patch_type", "replace")  # replace | append | patch

    if not target_file:
        result.success = False
        result.action_taken = "no_target_file"
        result.details = "proposed_change missing 'target_file'"
        return

    ws = _get_workspace(bot_id)
    if ws is None:
        result.success = False
        result.action_taken = "workspace_not_found"
        result.details = f"Cannot locate workspace for {bot_id}"
        return

    # Resolve target path (must be within workspace)
    if target_file.startswith("/"):
        target_path = Path(target_file)
    else:
        target_path = ws / target_file

    # Resolve symlinks before checking allowed roots (prevents symlink escape)
    try:
        resolved_path = target_path.resolve()
    except OSError:
        resolved_path = target_path

    # Safety: must be within this bot's workspace or shared evolve dir
    allowed_roots = [ws.resolve(), CANONICAL_SHARED_DIR.resolve()]
    if not any(resolved_path.is_relative_to(r) for r in allowed_roots):
        result.success = False
        result.action_taken = "path_outside_workspace"
        result.details = f"Target path {target_path} is outside allowed roots."
        return

    # Backup if file exists
    backup = None
    if target_path.exists():
        backup = target_path.with_suffix(target_path.suffix + f".bak.{int(time.time())}")
        shutil.copy2(target_path, backup)

    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)

        if patch_type == "append":
            with target_path.open("a") as f:
                f.write("\n" + new_content)
        else:  # replace
            target_path.write_text(new_content)

        result.action_taken = "script_written"
        result.details = f"Wrote {target_path.relative_to(ws)} ({len(new_content)} chars)"
        if backup:
            backup.unlink(missing_ok=True)
    except Exception as e:
        # Restore backup
        if backup and backup.exists():
            shutil.copy2(backup, target_path)
            result.rollback_triggered = True
        result.success = False
        result.action_taken = "script_write_failed"
        result.details = f"Failed to write {target_path}: {e}"


# ── workflow_change ───────────────────────────────────────────────────────────

def _apply_workflow_change(proposal: dict, bot_id: str, shared_dir: Path, result: ApplyResult) -> None:
    """
    Workflow changes can't be mechanically applied.
    Write a structured instruction note to the bot's workspace so it
    reads it at the start of the next session (via AGENTS.md or daily notes).
    """
    ws = _get_workspace(bot_id)
    if ws is None:
        result.success = False
        result.details = "Cannot locate workspace"
        return

    change = proposal.get("proposed_change", {})
    description = change.get("description", proposal.get("problem", ""))
    minimum_test = proposal.get("minimum_test", "")

    note_path = ws / "memory" / "evolve-workflow-instructions.md"
    note_path.parent.mkdir(parents=True, exist_ok=True)

    # Append to the instructions file
    with note_path.open("a") as f:
        f.write(f"\n## {now_iso()} — Evolve Workflow Instruction\n")
        f.write(f"**Proposal:** {proposal.get('id','?')}\n")
        f.write(f"**Change:** {description}\n")
        if minimum_test:
            f.write(f"**Verify by:** {minimum_test}\n")
        f.write(f"**Status:** Pending acknowledgment\n\n")

    result.action_taken = "workflow_note_written"
    result.details = f"Workflow instruction written to {note_path.name}. Bot will read on next session."


# ── Helpers ───────────────────────────────────────────────────────────────────

def _targets_bot(proposal_path: Path, bot_id: str) -> bool:
    try:
        data = json.loads(proposal_path.read_text())
        return data.get("target_bot") == bot_id
    except Exception:
        return False


def _get_workspace(bot_id: str) -> Path | None:
    oc_json = _bot_home(bot_id) / ".openclaw" / "openclaw.json"
    try:
        cfg = json.loads(oc_json.read_text())
        ws = cfg.get("agents", {}).get("defaults", {}).get("workspace")
        if ws:
            return Path(ws)
    except Exception:
        pass
    fallback = _bot_home(bot_id) / ".openclaw" / "workspace"
    return fallback if fallback.exists() else None


def _restart_gateway(bot_id: str) -> bool:
    """Restart this bot's gateway.

    apply.py runs as the bot user (e.g. team_bot_a), which cannot sudo to kickstart
    a system LaunchDaemon.  Instead we SIGTERM live (non-zombie) gateway
    processes owned by this user — launchd's KeepAlive will restart the service
    automatically.  Returns True if at least one live process was signalled,
    False if no gateway process was found (e.g. fresh setup, plist not yet
    created).  The caller is responsible for polling health after this returns.
    """
    import pwd
    uid = os.getuid()
    try:
        username = pwd.getpwuid(uid).pw_name
    except KeyError:
        username = bot_id

    # `pgrep openclaw-gateway` (without -f) only matches the legacy process
    # title (openclaw < 2026.4.29).  For newer versions the process is
    # `node .../openclaw/dist/entry.js`, so we scan `ps auxww` and filter
    # by the canonical marker list (defined in heal.py, kept in sync with
    # evolve_admin.ocadmin._GATEWAY_PROC_MARKERS).
    from heal import _is_gateway_proc_line

    # Find gateway PIDs owned by this user, filtering out zombies.
    # SIGTERM to a zombie succeeds (no error) but has no effect — launchd
    # never sees a death and won't restart.
    live_pids: list[int] = []
    try:
        proc = subprocess.run(
            ["ps", "auxww"], capture_output=True, text=True, timeout=5,
        )
        for line in proc.stdout.splitlines():
            if "grep" in line:
                continue
            if not _is_gateway_proc_line(line, username):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                pid = int(parts[1])
            except ValueError:
                continue
            # Check process state — skip zombies (stat starts with 'Z')
            try:
                stat_r = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "stat="],
                    capture_output=True, text=True,
                )
                if stat_r.returncode == 0 and not stat_r.stdout.strip().startswith("Z"):
                    live_pids.append(pid)
            except Exception:
                live_pids.append(pid)  # assume alive if check fails
    except Exception:
        pass

    if not live_pids:
        # No running gateway process — launchd may not have started it yet
        # (e.g. system plist not installed on fresh setup).
        return False

    for pid in live_pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    # Do NOT sleep here — the caller polls _check_health() in a retry loop,
    # which handles launchd backoff correctly.  A fixed sleep would either be
    # too short (launchd in backoff) or needlessly long (normal restart).
    return True


def _check_health(port: int, timeout: float = 5.0) -> bool:
    import urllib.request, urllib.error
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/evolve/status", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        # Fall back to basic gateway check
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=timeout) as r:
                return r.status == 200
        except Exception:
            return False


def _set_nested(obj: dict, parts: list[str], value) -> None:
    for p in parts[:-1]:
        if p not in obj or not isinstance(obj[p], dict):
            obj[p] = {}
        obj = obj[p]
    obj[parts[-1]] = value


def _write_bot_config(path: Path, data: dict) -> None:
    """Write a bot-owned config file. Uses /tmp staging + sudo cp when running as
    the evolve user (NOT the shared atomic-write primitive — that pattern cannot
    cross user-ownership boundaries; see evolve_util.atomic_write_json)."""
    import os
    current_user = os.environ.get("USER", "")
    if current_user == "evolve":
        # evolve user can't write directly to bot home dirs
        # use /tmp staging + sudo cp (requires sudoers grant)
        from staging import secure_stage

        bot_id = path.parts[2]  # /Users/{bot_id}/.openclaw/...
        tmp = secure_stage(json.dumps(data, indent=2))
        try:
            subprocess.run(["sudo", "/bin/cp", str(tmp), str(path)], check=True)
            subprocess.run(["sudo", "/usr/sbin/chown", f"{bot_id}:staff", str(path)], check=True)
        finally:
            tmp.unlink(missing_ok=True)
    else:
        # running as bot user — write directly (original behavior)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.rename(path)


def _quarantine_proposal(
    proposal: dict,
    proposal_path: Path,
    quarantine_dir: Path,
    results_dir: Path,
    bot_id: str,
    reason: str,
) -> None:
    """Move a proposal to quarantine/ and write a failed apply-result.

    Failure modes here are logged loudly because of the 2026-05-20 forensic
    case: ``heal-team_bot_a-1776294604`` was quarantined on Apr 25 but the unlink
    of the original ``approved/`` copy silently failed, leaving the proposal
    in approved/ for a full month while apply.py "Already applied — skipping"
    held it pinned via the apply-results entry. A silent ``except OSError:
    pass`` made that invisible. We now print the failure so the next run can
    be diagnosed instead of going stuck.
    """
    proposal_id = proposal.get("id", proposal_path.stem)
    dst = quarantine_dir / f"{proposal_id}.json"
    proposal["_quarantine_reason"] = reason
    tmp = dst.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(proposal, indent=2))
        tmp.rename(dst)
    except OSError as exc:
        # If we can't even create the quarantine record, do NOT delete the
        # original — that would leave the proposal unaccounted for entirely.
        # Bail and let the next cycle retry; the reviewer agent caught this
        # logic gap during the 2026-05-21 forensics second pass.
        print(
            f"[evolve/apply] _quarantine_proposal: failed to write {dst}: {exc} "
            f"— leaving {proposal_path} in place for next cycle to retry",
            file=sys.stderr,
        )
        return
    try:
        proposal_path.unlink(missing_ok=True)
    except OSError as exc:
        print(
            f"[evolve/apply] _quarantine_proposal: FAILED to unlink "
            f"{proposal_path} after quarantining {proposal_id}: {exc} "
            f"(proposal will stay in approved/ — stuck_proposal Signal will catch it)",
            file=sys.stderr,
        )
    _write_result(results_dir, ApplyResult(
        proposal_id=proposal_id, bot_id=bot_id, success=False,
        action_taken="quarantined",
        details=f"Signature verification failed: {reason}",
    ))


def _write_result(results_dir: Path, result: ApplyResult) -> None:
    path = results_dir / f"{result.proposal_id}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result.to_dict(), indent=2))
    tmp.rename(path)


def _trigger_regression_tests(bot_id: str, shared_dir: Path) -> None:
    """
    After a successful apply, run regression tests to confirm nothing broke.
    Runs asynchronously (background subprocess) so apply.py doesn't block.
    """
    test_script = Path(__file__).parent / "test_runner.py"
    if not test_script.exists():
        return
    try:
        subprocess.Popen(
            [sys.executable, str(test_script),
             "--bot-id", bot_id,
             "--mode", "pre-apply"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _register_outcome(proposal: dict, result: ApplyResult, bot_id: str, shared_dir: Path) -> None:
    """
    After a successful apply, write a pending outcome record so outcome.py
    can send a 7-day check-in to Pod_admin asking whether the change helped.
    """
    try:
        import uuid as _uuid
        from outcome import write_pending_outcome

        outcome_id = str(_uuid.uuid4())
        proposal_summary = proposal.get("problem", "applied change")[:120]
        detector_name = proposal.get("pattern_key", "").split(":", 1)[-1] if ":" in proposal.get("pattern_key", "") else ""

        pending_outcome = {
            "schema_version": 1,
            "outcome_id": outcome_id,
            "proposal_id": proposal.get("id", ""),
            "target_bot": bot_id,
            "applied_date": date.today().isoformat(),
            "proposal_summary": proposal_summary,
            "detector_name": detector_name,
            "proposal_type": proposal.get("type", ""),
            "checkin_sent": False,
            "checkin_sent_date": None,
        }
        write_pending_outcome(pending_outcome, str(shared_dir))
        print(f"  Outcome registered: {outcome_id[:8]} (check-in in 7 days)")
    except Exception as e:
        print(f"  Warning: could not register outcome: {e}")


def _alert(proposal: dict, result: ApplyResult, shared_dir: Path) -> None:
    """Push the apply-result notification via the alerts dispatcher.

    Phase F5: passes structured payload; catalog body_template renders
    "📋 Proposal {action} / {bot_id}: {proposal_id} / {problem_summary}"
    plus the ui_action ("Proposals → Recent"). Severity tracks the
    outcome: success = INFO, rollback/failure = WARNING.
    """
    network = load_json(shared_dir / "network.json") or {}
    severity_name = "info" if result.success else "warning"
    problem_summary = proposal.get("problem", "")[:100]
    if result.rollback_triggered:
        problem_summary = f"{problem_summary} (rolled back — config restored)"

    import sys as _sys
    try:
        from evolve_admin.alerts.dispatcher import (
            send as _dispatch_send, Severity,
        )
    except Exception as exc:
        print(f"[evolve/apply] dispatcher import failed; alert dropped: {exc}",
              file=_sys.stderr)
        return

    severity_map = {
        "critical": Severity.CRITICAL, "error": Severity.ERROR,
        "warning": Severity.WARNING, "info": Severity.INFO,
    }
    try:
        _dispatch_send(
            shared_dir=shared_dir,
            network=network,
            source="apply",
            severity=severity_map[severity_name],
            dedup_key=f"apply/{result.proposal_id}/{result.action_taken}",
            catalog_event="decisions.proposal_applied",
            payload={
                "action": result.action_taken,
                "bot_id": result.bot_id,
                "proposal_id": result.proposal_id,
                "problem_summary": problem_summary,
            },
        )
    except Exception as exc:
        print(f"[evolve/apply] dispatcher.send raised; alert dropped: {exc}",
              file=_sys.stderr)


def _resolve_shared_dir(network_path: Path | None, override: Path | None) -> Path:
    # Precedence: explicit override → the (platform-keyed) network.json's
    # sharedDir → the platform canonical default. network_path defaults to
    # resolve_network_path() (macOS /Users/Shared, Linux /var/lib), so we
    # NEVER probe a hardcoded /Users/Shared/evolve first — on a Linux pod a
    # stray /Users tree would otherwise win over the real /var/lib shared dir.
    if override:
        return override
    canonical = CANONICAL_SHARED_DIR
    if network_path and network_path.exists():
        try:
            data = json.loads(network_path.read_text())
            return Path(data.get("sharedDir", str(canonical)))
        except Exception:
            pass
    return canonical


_REQUIRED_PROPOSAL_FIELDS = ("id", "type", "target_bot", "proposed_change")
_VALID_PROPOSAL_TYPES = ("config_change", "script_change", "workflow_change", "investigation")


def _validate_proposal_schema(proposal: dict) -> str | None:
    """Return an error string if the proposal is missing required fields, else None."""
    for field in _REQUIRED_PROPOSAL_FIELDS:
        if field not in proposal:
            return f"missing required field '{field}'"
    if proposal["type"] not in _VALID_PROPOSAL_TYPES:
        return f"unknown type {proposal['type']!r}"
    if not isinstance(proposal.get("proposed_change"), dict):
        return "'proposed_change' must be a dict"
    return None


def load_json(path: Path | None) -> dict | list | None:
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(path)


def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


if __name__ == "__main__":
    main()
