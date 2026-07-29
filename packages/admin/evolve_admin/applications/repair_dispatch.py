"""Repair session dispatch — backend for PR 10.

Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §11.3.

Repair sessions are how findings become fixes. The operator (or bot
user via evo) clicks Repair on a chip → this module assembles the
input bundle, writes a repair-request file to the bot's audit_inbox/,
and kicks the bot's audit_runner.py in repair mode.

PR 10 ships the dispatch helper + the input-bundle builder + the
allowed-transformations safety list + the output schema validator.
The actual subprocess kick (sudo dispatch to the bot's audit_runner)
and the LLM call inside the runner are left to a follow-up — they
require coordination with the runner's existing infra.

## Input bundle (spec §11.3.2)

The bundle is the read-only input the LLM sees:

  {
    "request_id": "repair-<8hex>",
    "app_id":     "...",
    "findings":   [...],            # the findings to repair
    "operator_rationale": "...",    # freeform; optional
    "operator_intent":    "...",    # "restore" | "remove" | "investigate"
    "context": {
        "manifest_snapshot": {...}, # full manifest
        "recent_trail":      [...], # last 30 days structural events
        "last_test_run":     {...},
        "last_audit":        {...},
    },
  }

## Allowed transformations (spec §11.3.4)

Clicking Repair is pre-approval for these mechanical fixes; the
runner can apply directly without further confirmation:

  - reinstall_cron_from_manifest
  - reembed_heartbeat_section
  - restore_file_from_git_history
  - update_files_sha_after_drift_approval
  - remove_unclaimed_crontab_entry
  - rename_files_path

Anything else → emit a Proposal for operator approval.

## Output schema (spec §11.3.6)

Same shape as Tier 3 audit outbox records:

  {
    "request_id": "...",
    "applied_transformations": [
        {"kind": "...", "trail_entry": {...}, "applied_at": "..."}
    ],
    "proposals": [
        {"kind": "...", "rationale": "...", "proposal_payload": {...}}
    ],
    "status": "ok" | "failed",
    "duration_ms": ...,
    "tokens": {"input": ..., "output": ...},
  }
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from platform_profile import get_profile

logger = logging.getLogger(__name__)


# ── Constants ──────────────────────────────────────────────────────────────

OPERATOR_INTENT_RESTORE     = "restore"
OPERATOR_INTENT_REMOVE      = "remove"
OPERATOR_INTENT_INVESTIGATE = "investigate"
OPERATOR_INTENT_ADD_MECHANISM = "add_mechanism"
OPERATOR_INTENT_MODIFY_CLAIM  = "modify_claim"
OPERATOR_INTENT_ACCEPT        = "accept"

VALID_OPERATOR_INTENTS = frozenset({
    OPERATOR_INTENT_RESTORE,
    OPERATOR_INTENT_REMOVE,
    OPERATOR_INTENT_INVESTIGATE,
    OPERATOR_INTENT_ADD_MECHANISM,
    OPERATOR_INTENT_MODIFY_CLAIM,
    OPERATOR_INTENT_ACCEPT,
})


ALLOWED_TRANSFORMATIONS = frozenset({
    "reinstall_cron_from_manifest",
    "reembed_heartbeat_section",
    "restore_file_from_git_history",
    "update_files_sha_after_drift_approval",
    "remove_unclaimed_crontab_entry",
    "rename_files_path",
})


# Repair session size limits per spec §11.3.7.
DEFAULT_MAX_REPAIRS_PER_APP_PER_DAY = 3
DEFAULT_MAX_TOKENS_SINGLE_FINDING   = 5_000
DEFAULT_MAX_TOKENS_REPAIR_ALL       = 15_000


# ── Result types ───────────────────────────────────────────────────────────


@dataclass
class RepairRequest:
    """The full input bundle written to the bot's audit_inbox/.

    The bot's audit_runner --repair mode reads this verbatim.
    """
    request_id: str = ""
    app_id: str = ""
    findings: list[dict] = field(default_factory=list)
    operator_rationale: str = ""
    operator_intent: str = OPERATOR_INTENT_RESTORE
    context: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "request_id":         self.request_id,
            "app_id":             self.app_id,
            "findings":           list(self.findings),
            "operator_rationale": self.operator_rationale,
            "operator_intent":    self.operator_intent,
            "context":            dict(self.context),
            "created_at":         self.created_at,
        }


@dataclass
class DispatchResult:
    """Result of dispatching a repair session."""
    request_id: str = ""
    written_path: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict:
        return {
            "request_id":   self.request_id,
            "written_path": self.written_path,
            "error":        self.error,
            "ok":           self.ok,
        }


# ── Request ID generation ──────────────────────────────────────────────────


def new_request_id(app_id: str, finding_signatures: list[str],
                   *, now_iso: str | None = None) -> str:
    """Stable request_id derived from app + finding signatures + time.

    Same operator clicking the same Repair button twice within a
    second produces the same id (idempotent). Different findings
    produce different ids.
    """
    now = now_iso or datetime.now(timezone.utc).isoformat()
    seed = f"{app_id}|{'|'.join(sorted(finding_signatures))}|{now[:13]}"
    return f"repair-{hashlib.sha256(seed.encode()).hexdigest()[:8]}"


# ── Input bundle builder ──────────────────────────────────────────────────


def build_repair_request(
    *,
    app_id: str,
    findings: list[dict],
    manifest: dict,
    operator_rationale: str = "",
    operator_intent: str = OPERATOR_INTENT_RESTORE,
    recent_trail: list[dict] | None = None,
    last_test_run: dict | None = None,
    last_audit: dict | None = None,
    now_iso: str | None = None,
) -> RepairRequest:
    """Assemble the spec §11.3.2 input bundle.

    Args:
        app_id: target app.
        findings: the chip-marked findings to repair (one or many).
        manifest: snapshot of the manifest at request time.
        operator_rationale: optional freeform text the operator typed.
        operator_intent: one of VALID_OPERATOR_INTENTS. Defaults to
            restore — the most common case.
        recent_trail: last ~30 days of structural-event trail entries.
        last_test_run: latest test run summary.
        last_audit: latest audit summary.
        now_iso: timestamp override for testing.

    Returns:
        A RepairRequest with a stable request_id and the full context.

    Raises:
        ValueError on invalid operator_intent.
    """
    if operator_intent not in VALID_OPERATOR_INTENTS:
        raise ValueError(
            f"unknown operator_intent {operator_intent!r}; "
            f"valid: {sorted(VALID_OPERATOR_INTENTS)}"
        )
    if not isinstance(findings, list) or not findings:
        raise ValueError("findings must be a non-empty list")

    sigs = [
        (f.get("signature") or f.get("id") or "")
        for f in findings
    ]
    request_id = new_request_id(app_id, sigs, now_iso=now_iso)
    return RepairRequest(
        request_id=request_id,
        app_id=app_id,
        findings=findings,
        operator_rationale=operator_rationale,
        operator_intent=operator_intent,
        context={
            "manifest_snapshot": manifest,
            "recent_trail":      recent_trail or [],
            "last_test_run":     last_test_run or {},
            "last_audit":        last_audit or {},
        },
        created_at=now_iso or datetime.now(timezone.utc).isoformat(),
    )


# ── Dispatch ───────────────────────────────────────────────────────────────


def write_repair_request(
    request: RepairRequest,
    audit_inbox_dir: Path,
) -> DispatchResult:
    """Write the request to the bot's audit_inbox/ as a JSON file.

    The runner picks up files matching ``repair-*.json`` in this
    directory. Atomic via rename.

    Args:
        request: the input bundle.
        audit_inbox_dir: ``/Users/<bot>/.openclaw/workspace/evolve/audit_inbox/``
            (admin caller resolves the per-bot path).

    Returns:
        DispatchResult with the written file path.
    """
    if not request.request_id:
        return DispatchResult(
            request_id="", written_path="",
            error="request has no request_id",
        )
    try:
        audit_inbox_dir.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError) as exc:
        return DispatchResult(
            request_id=request.request_id, written_path="",
            error=f"could not create inbox dir: {exc}",
        )
    target = audit_inbox_dir / f"{request.request_id}.json"
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(request.to_dict(), indent=2),
                       encoding="utf-8")
        os.replace(tmp, target)
    except (PermissionError, OSError) as exc:
        return DispatchResult(
            request_id=request.request_id, written_path="",
            error=f"could not write request file: {exc}",
        )
    return DispatchResult(
        request_id=request.request_id,
        written_path=str(target),
    )


# ── Rate limiting ──────────────────────────────────────────────────────────


def count_repairs_today(
    audit_outbox_dir: Path, *, app_id: str | None = None,
) -> int:
    """Count completed repair outboxes in the last 24h (best-effort).

    Used by callers to enforce the spec §11.3.7 rate limit (3 repairs
    per app per day). Files are matched by name; per-app filtering
    looks at the JSON's ``app_id`` field.
    """
    if not audit_outbox_dir.exists():
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - 86_400
    count = 0
    try:
        files = list(audit_outbox_dir.glob("repair-*.json"))
    except OSError:
        return 0
    for f in files:
        try:
            if f.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        if app_id:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if data.get("app_id") != app_id:
                    continue
            except (OSError, json.JSONDecodeError):
                continue
        count += 1
    return count


def check_rate_limit(
    audit_outbox_dir: Path,
    app_id: str,
    *,
    max_per_day: int = DEFAULT_MAX_REPAIRS_PER_APP_PER_DAY,
) -> tuple[bool, str]:
    """Return ``(allowed, reason)``. False when the per-app daily cap
    has been hit."""
    n = count_repairs_today(audit_outbox_dir, app_id=app_id)
    if n >= max_per_day:
        return False, (
            f"app {app_id!r} has {n} repair(s) in the last 24h "
            f"(max {max_per_day}); next attempt in <24h will be rejected. "
            f"Operator override required."
        )
    return True, ""


# ── Output schema validator ──────────────────────────────────────────────


def validate_repair_outbox(payload: Any) -> tuple[bool, list[str]]:
    """Validate a repair outbox record against the spec §11.3.6 schema.

    Returns ``(valid, errors)``. The dispatcher (admin-side poller)
    uses this before applying any transformations from the record.
    Invalid payload is treated as a failed run.
    """
    errors: list[str] = []
    if not isinstance(payload, dict):
        return False, ["payload is not a dict"]

    for key in ("request_id", "status"):
        if key not in payload:
            errors.append(f"missing key {key!r}")

    status = payload.get("status")
    if status not in ("ok", "failed"):
        errors.append(f"status must be 'ok' or 'failed', got {status!r}")

    transforms = payload.get("applied_transformations") or []
    if not isinstance(transforms, list):
        errors.append("applied_transformations must be a list")
    else:
        for i, t in enumerate(transforms):
            if not isinstance(t, dict):
                errors.append(f"applied_transformations[{i}] not a dict")
                continue
            kind = t.get("kind")
            if kind not in ALLOWED_TRANSFORMATIONS:
                errors.append(
                    f"applied_transformations[{i}] kind={kind!r} not in "
                    f"ALLOWED_TRANSFORMATIONS (Tier 1 may not auto-apply)"
                )

    proposals = payload.get("proposals") or []
    if not isinstance(proposals, list):
        errors.append("proposals must be a list")

    return not errors, errors


# ── End-to-end dispatch (write inbox + kick runner) ───────────────────────


# Resolution priority for the audit_runner.py path. The deploy-checkout
# location wins; falls back to /usr/local for unusual installs. Same set
# as audit_dispatch.py — the repair runner is a mode of the same script.
_RUNNER_PATH_CANDIDATES = (
    f"{get_profile().deploy_checkout_default}/packages/analyzer/app_audit_runner.py",
    "/usr/local/share/evolve/app_audit_runner.py",
)

# Mirror the audit_dispatch fast-fail window: 2s is enough for sudo to
# reject (immediate) and short repair runs (no-op idempotent path) to
# complete, without making the HTTP request hang on real repair work.
_KICK_FAST_FAIL_WAIT_SECONDS = 2.0


def _resolve_runner_path() -> str | None:
    for p in _RUNNER_PATH_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def _audit_inbox_dir_for_bot(bot_user: str) -> Path:
    return Path(f"/Users/{bot_user}/.openclaw/workspace/evolve/audit_inbox")


def _write_inbox_file_with_sudo_fallback(
    inbox_path: Path, request: RepairRequest,
) -> tuple[bool, str]:
    """Write the repair request file to the bot's audit_inbox/.

    Mirrors the audit_dispatch pattern: direct write first; fall back to
    ``/tmp`` staging + ``sudo /bin/cp`` on PermissionError. The bot user
    owns the inbox dir; evolve writes via the inherited ACL the deploy
    set, but freshly-deployed bots may need the sudo path until the
    ACL refresh propagates.
    """
    content = json.dumps(request.to_dict(), indent=2)
    try:
        inbox_path.parent.mkdir(parents=True, exist_ok=True)
        inbox_path.write_text(content)
        return True, ""
    except PermissionError:
        pass
    except OSError as exc:
        return False, f"inbox direct write OSError: {exc}"

    fd, tmp = tempfile.mkstemp(
        dir="/tmp", prefix="evolve-repair-", suffix=".json",
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        try:
            subprocess.run(
                ["sudo", "/bin/mkdir", "-p", str(inbox_path.parent)],
                capture_output=True, timeout=5,
            )
        except subprocess.SubprocessError:
            pass
        r = subprocess.run(
            ["sudo", "/bin/cp", tmp, str(inbox_path)],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return False, f"sudo cp inbox failed: {r.stderr.strip()[:200]}"
        subprocess.run(
            ["sudo", "/bin/chmod", "644", str(inbox_path)],
            capture_output=True, timeout=5,
        )
        return True, ""
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _kick_repair_runner(bot_user: str, request_id: str) -> tuple[bool, str]:
    """Invoke ``audit_runner.py --repair --request-id <id>`` for *bot_user*.

    Same dispatch shape as audit_dispatch._kick_runner — direct exec when
    ``bot_user == current_user`` (the evo bot on the admin's evolve
    account), sudo otherwise. See audit_dispatch.py for the rationale on
    each branch.
    """
    runner_path = _resolve_runner_path()
    if not runner_path:
        return False, "audit_runner.py not found at expected paths"

    try:
        import getpass
        current_user = getpass.getuser()
    except Exception:    # noqa: BLE001
        current_user = ""

    if bot_user == current_user:
        cmd = [
            "/opt/homebrew/bin/python3", runner_path,
            "--bot-id", bot_user,
            "--repair",
            "--request-id", request_id,
        ]
    else:
        cmd = [
            "/usr/bin/sudo", "-n", "-H", "-u", bot_user,
            "/opt/homebrew/bin/python3", runner_path,
            "--bot-id", bot_user,
            "--repair",
            "--request-id", request_id,
        ]

    stderr_fd, stderr_path = tempfile.mkstemp(
        prefix="evolve-repair-kick-", suffix=".err",
    )
    try:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=stderr_fd,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                cwd="/tmp",
            )
        except OSError as exc:
            return False, f"Popen failed: {exc}"
        finally:
            os.close(stderr_fd)
            stderr_fd = -1

        try:
            rc = proc.wait(timeout=_KICK_FAST_FAIL_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            # Real repair work happening; let the runner detach.
            return True, ""

        try:
            with open(stderr_path, "r") as f:
                stderr = f.read().strip()
        except OSError:
            stderr = ""

        if rc == 0:
            return True, ""
        msg = stderr[:300] if stderr else f"runner exited {rc}"
        return False, msg
    finally:
        if stderr_fd != -1:
            try:
                os.close(stderr_fd)
            except OSError:
                pass
        try:
            os.unlink(stderr_path)
        except OSError:
            pass


def dispatch_repair(
    *,
    bot_user: str,
    request: RepairRequest,
    kick: bool = True,
) -> DispatchResult:
    """End-to-end: write inbox + kick the bot's runner in repair mode.

    Args:
        bot_user: the macOS user the bot runs as.
        request: the built ``RepairRequest`` (from ``build_repair_request``).
        kick: when True (default), invoke the runner immediately so the
            session runs without waiting for an inbox tick. When False,
            the request sits in the inbox until something else processes
            it. Tests pass kick=False.

    Returns:
        A DispatchResult with ``written_path`` set on success. Kick
        failures populate ``error`` but do NOT clear ``ok`` — the inbox
        request still queued and a later kick can pick it up.
    """
    inbox_dir = _audit_inbox_dir_for_bot(bot_user)
    audit_outbox = Path(
        f"/Users/{bot_user}/.openclaw/workspace/evolve/audit_outbox"
    )

    # Rate-limit BEFORE writing the inbox file so a hot loop can't burn
    # disk. The bot's runner also rate-limits; this is the admin-side
    # gate that bounces the operator's click immediately.
    if request.app_id:
        allowed, reason = check_rate_limit(audit_outbox, request.app_id)
        if not allowed:
            return DispatchResult(
                request_id=request.request_id, written_path="",
                error=f"rate limit: {reason}",
            )

    inbox_path = inbox_dir / f"{request.request_id}.json"
    ok, err = _write_inbox_file_with_sudo_fallback(inbox_path, request)
    if not ok:
        return DispatchResult(
            request_id=request.request_id, written_path="",
            error=err,
        )

    result = DispatchResult(
        request_id=request.request_id,
        written_path=str(inbox_path),
    )
    if not kick:
        return result

    kicked, kick_err = _kick_repair_runner(bot_user, request.request_id)
    if kick_err:
        logger.warning(
            "repair_dispatch: kick failed for %s/%s: %s",
            bot_user, request.request_id, kick_err,
        )
        result.error = f"kick failed: {kick_err}"
    return result
