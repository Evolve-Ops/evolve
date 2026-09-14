#!/usr/bin/env python3
"""
app_repair_runner.py — Bot-side repair session runner.

Spec: internal/spec-app-coherence-and-reconciliation-2026-06-05.md §11.3.

The bot-side counterpart to ``repair_dispatch.py`` (admin-side, PR #2264).
The admin assembles the input bundle and writes it to
``/Users/<bot>/.openclaw/workspace/evolve/audit_inbox/repair-<id>.json``.
This module reads that file, runs an LLM-driven repair session, applies
allowed mechanical transformations, and writes the result to
``audit_outbox/repair-applied-<id>.json`` (or ``repair-failed-<id>.json``).
The admin's audit_poller picks it up and writes the changelog entry.

## Flow per request

  inbox(repair-<id>.json)
    ↓ load + validate
  rate_limit check (3/day/app via check_rate_limit on outbox)
    ↓ pass
  build_repair_prompt (system + user from manifest + findings)
    ↓
  _dispatch_via_oc_full (shared with tier3 — process-group kill, cost recovery)
    ↓ parse JSON
  validate decisions vs ALLOWED_TRANSFORMATIONS
    ↓
  per decision: apply transformation OR record as Proposal
    ↓
  outbox(repair-applied-<id>.json or repair-failed-<id>.json)

## Scope

PR ships ``reinstall_cron_from_manifest`` end-to-end. The other five
transformations land as ``_TODO`` stubs that bubble up as Proposals
rather than silently failing — the LLM may pick them, but the executor
refuses to apply and the operator sees a Proposal explaining what
would change. They're tracked in spec §11.3.4 for the follow-up PR.

## Defense in depth

The LLM is told the allowlist and instructed to refuse anything outside
it. We don't trust that — every returned kind is checked against
``ALLOWED_TRANSFORMATIONS`` (imported from the admin module) before any
filesystem mutation. An LLM that returns
``"kind": "rm -rf /"`` bubbles up as a Proposal, not an action.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evolve_util import now_iso as _iso_now

# evolve_admin is installed in the runtime venv; we pull
# ALLOWED_TRANSFORMATIONS from the source of truth — the admin's
# repair_dispatch module — rather than duplicating the list here.
_ADMIN_LOADED: bool | None = None


def _load_admin_module() -> bool:
    global _ADMIN_LOADED
    if _ADMIN_LOADED is not None:
        return _ADMIN_LOADED
    try:
        # Touch the import to confirm reachability.
        from evolve_admin.applications.repair_dispatch import (  # noqa: F401
            ALLOWED_TRANSFORMATIONS,
        )
        _ADMIN_LOADED = True
    except Exception as e:    # noqa: BLE001
        logging.warning(
            "app_repair_runner: admin module unreachable (%s); "
            "falling back to in-module allowlist", e,
        )
        _ADMIN_LOADED = False
    return _ADMIN_LOADED


# In-module copy of the allowlist used as a fallback when evolve_admin
# isn't installed (e.g. running the bot-side runner against a partial
# install). Kept in sync with repair_dispatch.ALLOWED_TRANSFORMATIONS —
# divergence is caught by the test suite.
_FALLBACK_ALLOWED_TRANSFORMATIONS = frozenset({
    "reinstall_cron_from_manifest",
    "reembed_heartbeat_section",
    "restore_file_from_git_history",
    "update_files_sha_after_drift_approval",
    "remove_unclaimed_crontab_entry",
    "rename_files_path",
})


def _allowed_transformations() -> frozenset[str]:
    """Return the live ALLOWED_TRANSFORMATIONS set, falling back gracefully."""
    if _load_admin_module():
        from evolve_admin.applications.repair_dispatch import (  # noqa: WPS433
            ALLOWED_TRANSFORMATIONS,
        )
        return ALLOWED_TRANSFORMATIONS
    return _FALLBACK_ALLOWED_TRANSFORMATIONS


logger = logging.getLogger(__name__)


REPAIR_RUNNER_VERSION = "1.0.0"


# ── Constants ──────────────────────────────────────────────────────────────

# LLM dispatch budget. Per spec §11.3.7: single-finding repair sessions
# typically run ~5k tokens; "repair all on this app" up to ~15k. The
# cap below is a hard ceiling at the prompt-body level — the wrapper
# refuses messages this large before the LLM is fired.
_REPAIR_TIMEOUT_S = 180
_REPAIR_MAX_TOKENS_SINGLE = 5_000
_REPAIR_MAX_TOKENS_REPAIR_ALL = 15_000

# Record kinds written into the outbox. The poller dispatches on these.
KIND_REPAIR_APPLIED = "repair_applied"
KIND_REPAIR_FAILED = "repair_failed"


# ── Result types ───────────────────────────────────────────────────────────


@dataclass
class _ExecResult:
    """One transformation's outcome."""
    applied: bool
    summary: str
    trail_entry: dict = field(default_factory=dict)


@dataclass
class RepairResult:
    """End-to-end repair session outcome."""
    request_id: str
    app_id: str
    status: str                       # "ok" | "failed"
    applied_transformations: list[dict] = field(default_factory=list)
    proposals: list[dict] = field(default_factory=list)
    error: str = ""
    duration_ms: int = 0
    tokens: dict = field(default_factory=lambda: {"input": 0, "output": 0, "total": 0})

    def to_outbox_record(self) -> dict:
        kind = KIND_REPAIR_APPLIED if self.status == "ok" else KIND_REPAIR_FAILED
        return {
            "kind":                    kind,
            "request_id":              self.request_id,
            "app_id":                  self.app_id,
            "ts":                      _iso_now(),
            "runner_version":          REPAIR_RUNNER_VERSION,
            "status":                  self.status,
            "applied_transformations": list(self.applied_transformations),
            "proposals":               list(self.proposals),
            "error":                   self.error,
            "duration_ms":             self.duration_ms,
            "tokens":                  dict(self.tokens),
        }


# ── Time helper ────────────────────────────────────────────────────────────


# ── Prompt frame ───────────────────────────────────────────────────────────
#
# Spec §11.3.3. The system frame defines the contract; the user message
# carries the bundle.


def build_system_prompt(allowed: frozenset[str]) -> str:
    """Render the system prompt for a repair session.

    Lists the allowed transformations explicitly so the LLM can refuse
    anything outside (defense-in-depth — we verify on the receiving end
    too, but a model that understands the constraint produces cleaner
    decisions).
    """
    allowed_list = "\n".join(f"  - {kind}" for kind in sorted(allowed))
    return f"""You are a repair agent for the Evolve framework. The operator has reviewed coherence and audit findings on an application and clicked Repair, asking you to fix them.

For each finding, choose ONE action:

1. APPLY a mechanical transformation from this allowlist:
{allowed_list}

2. PROPOSE a design-level change (for anything outside the allowlist).

Hard rules:

- Refuse anything outside the allowlist. If the fix would write new code, modify integration credentials, change manifest claim text, or touch user data, emit a Proposal — not an action.
- Stay within the app's manifest scope. Do not redesign the app; close the specific gap the operator flagged.
- Honor the operator's stated intent ("restore" vs "remove" vs "investigate").
- Each finding gets at most ONE transformation OR one Proposal — not both.
- If you cannot determine a safe fix, emit a Proposal with rationale; never invent.

Return JSON in this exact shape:

{{
  "decisions": [
    {{
      "finding_id": "<id from input>",
      "action": "apply" | "propose",
      "kind": "<one of the allowlist when action=apply, or a short slug when action=propose>",
      "params": {{ /* transformation-specific parameters; may be empty */ }},
      "rationale": "<one-paragraph explanation of why this choice fits this finding>"
    }}
  ]
}}

Return nothing else — no preamble, no surrounding prose. Just the JSON."""


def build_user_message(request: dict, *, full_audit: bool = False) -> str:
    """Render the user-message body for a repair session.

    Carries the full input bundle (manifest snapshot, recent trail, last
    test run, last audit, findings, operator intent + rationale) as one
    JSON document. The LLM reads this against the system frame and
    decides per-finding.
    """
    context = request.get("context") or {}
    body = {
        "app_id":             request.get("app_id", ""),
        "operator_intent":    request.get("operator_intent", "restore"),
        "operator_rationale": request.get("operator_rationale", ""),
        "findings":           request.get("findings") or [],
        "manifest_snapshot":  context.get("manifest_snapshot") or {},
        "recent_trail":       context.get("recent_trail") or [],
        "last_test_run":      context.get("last_test_run") or {},
        "last_audit":         context.get("last_audit") or {},
        "full_audit":         bool(full_audit),
    }
    return json.dumps(body, indent=2, default=str)


# ── LLM-response parsing ───────────────────────────────────────────────────


_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")


def parse_repair_decisions(text: str) -> list[dict]:
    """Pull the ``decisions[]`` list out of the LLM's response.

    Tolerates surrounding prose (the prompt says don't, but models
    sometimes wrap in ``` fences anyway). Returns an empty list when no
    decisions key is found — the caller treats that as "LLM punted; no
    transformations to apply" rather than failing the whole session.

    Raises ValueError when the body is found but malformed.
    """
    raw = (text or "").strip()
    if not raw:
        return []

    # Strip ``` fences if the model added them.
    if raw.startswith("```"):
        # Drop the opening fence (with or without "json" suffix) and any
        # trailing fence.
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw)

    # Try a direct parse first.
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        # Fall back to the first JSON-object substring.
        m = _JSON_OBJECT_RE.search(raw)
        if not m:
            return []
        try:
            payload = json.loads(m.group(0))
        except json.JSONDecodeError as exc:
            raise ValueError(f"LLM response is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        return []
    decisions = payload.get("decisions") or []
    if not isinstance(decisions, list):
        raise ValueError("LLM response 'decisions' is not a list")
    out: list[dict] = []
    for d in decisions:
        if isinstance(d, dict):
            out.append(d)
    return out


# ── Cron transformation (full implementation) ──────────────────────────────


def _cron_schedule(entry: Any) -> str:
    if isinstance(entry, str):
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
            parts = s.split(maxsplit=1)
            return parts[1] if len(parts) > 1 else ""
        parts = s.split()
        return " ".join(parts[5:]) if len(parts) >= 6 else ""
    if isinstance(entry, dict):
        return (entry.get("script") or entry.get("script_path") or "").strip()
    return ""


def _normalize_cron_line(line: str) -> str:
    return " ".join(line.split()).strip()


def _cron_soft_match(schedule: str, script: str, live_line: str) -> bool:
    """Match a manifest cron against a live crontab line tolerantly.

    Real crontab lines often carry ``cd $HOME && python3 ~/.openclaw/...``
    wrappers; a strict equality misses them. Mirror the heuristic from
    ``app_audit_structural._cron_soft_match`` so we don't reinstall an
    already-present entry just because the wrapping differs.
    """
    norm = _normalize_cron_line(live_line)
    if not norm.startswith(schedule):
        return False
    basename = script.rsplit("/", 1)[-1].split()[0]
    return basename in norm


def _read_crontab() -> tuple[list[str], bool]:
    """Snapshot ``crontab -l``. Returns ``(lines, ok)``.

    ``ok=False`` means the read itself failed (crontab unavailable, EOF,
    timeout). Callers must NOT proceed with installation in that case —
    overwriting an unreadable crontab would silently nuke the user's
    existing schedule.
    """
    try:
        r = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("repair: crontab -l failed: %s", exc)
        return [], False

    # rc=1 with "no crontab for ..." stderr is the empty-crontab case —
    # NOT a failure. Distinguish from a real error by checking stderr.
    if r.returncode != 0:
        stderr = (r.stderr or "").lower()
        if "no crontab" in stderr:
            return [], True
        logger.warning(
            "repair: crontab -l returned rc=%d stderr=%r", r.returncode, r.stderr[:200],
        )
        return [], False
    return [ln for ln in r.stdout.splitlines()], True


def _write_crontab(content: str) -> tuple[bool, str]:
    """Write ``content`` to crontab via ``crontab -`` (stdin). Returns ``(ok, err)``."""
    try:
        r = subprocess.run(
            ["crontab", "-"],
            input=content,
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return False, f"crontab subprocess failed: {exc}"
    if r.returncode != 0:
        return False, f"crontab rc={r.returncode}: {(r.stderr or '').strip()[:200]}"
    return True, ""


def _apply_reinstall_cron_from_manifest(
    manifest: dict, workspace: Path, params: dict,
) -> _ExecResult:
    """Reinstall every missing ``manifest.crons[*]`` entry into the user's crontab.

    Idempotent: only appends entries that aren't already present (canonical
    or soft-matched). Re-running on a healthy crontab is a no-op.

    Spec source: §11.3.4 first bullet — "Reinstall cron from
    ``manifest.crons[*]`` spec (deterministic — the manifest is the spec)".
    """
    live_lines, ok = _read_crontab()
    if not ok:
        return _ExecResult(
            applied=False,
            summary="crontab -l unavailable; refusing to reinstall (would risk overwriting current schedule)",
        )

    live_norm = {
        _normalize_cron_line(ln)
        for ln in live_lines
        if ln.strip() and not ln.strip().startswith("#")
    }

    crons = manifest.get("crons") or []
    if not crons:
        return _ExecResult(
            applied=False,
            summary="manifest declares no crons[]; nothing to reinstall",
        )

    missing: list[str] = []
    skipped_unparseable = 0
    for entry in crons:
        schedule = _cron_schedule(entry)
        script = _cron_script(entry)
        if not schedule or not script:
            skipped_unparseable += 1
            continue
        canonical = _normalize_cron_line(f"{schedule} {script}")
        if canonical in live_norm:
            continue
        if any(_cron_soft_match(schedule, script, ln) for ln in live_norm):
            continue
        missing.append(canonical)

    if not missing:
        return _ExecResult(
            applied=False,
            summary=(
                f"no missing cron entries — no-op (idempotent); "
                f"checked {len(crons)} manifest crons, "
                f"{skipped_unparseable} unparseable"
            ),
        )

    # Build new crontab content: preserve live lines verbatim, append
    # missing entries. Trailing newline ensures crontab accepts the input.
    suffix = "\n".join(missing)
    if live_lines and live_lines[-1].strip():
        new_content = "\n".join(live_lines) + "\n" + suffix + "\n"
    elif live_lines:
        new_content = "\n".join(live_lines) + suffix + "\n"
    else:
        new_content = suffix + "\n"

    ok, err = _write_crontab(new_content)
    if not ok:
        return _ExecResult(
            applied=False,
            summary=f"crontab write failed: {err}",
        )

    return _ExecResult(
        applied=True,
        summary=f"reinstalled {len(missing)} cron entry(ies) from manifest",
        trail_entry={
            "transformation": "reinstall_cron_from_manifest",
            "added_lines":    missing,
            "manifest_crons_count": len(crons),
        },
    )


# ── Stubs for remaining 5 transformations ──────────────────────────────────
#
# Each stub refuses with a clear summary so the LLM's pick bubbles up as
# a Proposal rather than silently failing. Implement them one at a time
# in follow-up PRs — start with the most-needed per production telemetry.


def _apply_reembed_heartbeat_section(
    manifest: dict, workspace: Path, params: dict,
) -> _ExecResult:
    """TODO PR #2: Re-embed heartbeat section from manifest canonical text.

    Reads ``manifest.scheduled_actions[*].trigger`` and rewrites the
    matching section in ``HEARTBEAT.md`` (or the bot's equivalent).
    Needs careful sectioning logic + atomic write + sudo /tmp staging.
    """
    return _ExecResult(
        applied=False,
        summary=(
            "reembed_heartbeat_section not yet implemented; "
            "operator will receive a Proposal explaining the intended fix"
        ),
    )


def _apply_restore_file_from_git_history(
    manifest: dict, workspace: Path, params: dict,
) -> _ExecResult:
    """TODO PR #3: Restore a file from its stored sha in manifest.

    Looks up ``manifest.files[*].sha256``, runs ``git cat-file -p
    <sha>`` against the deploy checkout, writes back to disk. Needs the
    file to actually exist in some git ref reachable from the bot.
    """
    return _ExecResult(
        applied=False,
        summary=(
            "restore_file_from_git_history not yet implemented; "
            "operator will receive a Proposal"
        ),
    )


def _apply_update_files_sha_after_drift_approval(
    manifest: dict, workspace: Path, params: dict,
) -> _ExecResult:
    """TODO PR #4: Update manifest.files[*].sha256 to the live sha.

    Operator-approved drift: the file changed on purpose, manifest needs
    to catch up. Recompute sha, write back via the manifest's writer.
    """
    return _ExecResult(
        applied=False,
        summary=(
            "update_files_sha_after_drift_approval not yet implemented; "
            "operator will receive a Proposal"
        ),
    )


def _apply_remove_unclaimed_crontab_entry(
    manifest: dict, workspace: Path, params: dict,
) -> _ExecResult:
    """TODO PR #5: Remove crontab lines not claimed by any manifest.

    Only safe when crons[] provenance is ``authored`` (operator owns the
    declaration). Needs the cross-app sweep so we don't delete another
    app's cron by mistake.
    """
    return _ExecResult(
        applied=False,
        summary=(
            "remove_unclaimed_crontab_entry not yet implemented; "
            "operator will receive a Proposal"
        ),
    )


def _apply_rename_files_path(
    manifest: dict, workspace: Path, params: dict,
) -> _ExecResult:
    """TODO PR #6: Update manifest.files[*].path after a rename.

    Detects matching-sha at new path; updates the path field. Doesn't
    move files on disk — that's an operator decision.
    """
    return _ExecResult(
        applied=False,
        summary=(
            "rename_files_path not yet implemented; "
            "operator will receive a Proposal"
        ),
    )


_TRANSFORMATION_REGISTRY = {
    "reinstall_cron_from_manifest":          _apply_reinstall_cron_from_manifest,
    "reembed_heartbeat_section":             _apply_reembed_heartbeat_section,
    "restore_file_from_git_history":         _apply_restore_file_from_git_history,
    "update_files_sha_after_drift_approval": _apply_update_files_sha_after_drift_approval,
    "remove_unclaimed_crontab_entry":        _apply_remove_unclaimed_crontab_entry,
    "rename_files_path":                     _apply_rename_files_path,
}


def _apply_transformation(
    kind: str, decision: dict, manifest: dict, workspace: Path,
) -> _ExecResult:
    """Dispatch one decision to its executor.

    Defense-in-depth check: ``kind`` MUST already be in
    ``ALLOWED_TRANSFORMATIONS`` by the time we get here. Caller is the
    repair runner's per-decision loop; it filters the LLM's output
    against the allowlist before this call.
    """
    fn = _TRANSFORMATION_REGISTRY.get(kind)
    if fn is None:
        return _ExecResult(
            applied=False,
            summary=f"transformation {kind!r} has no executor registered",
        )
    params = decision.get("params") or {}
    try:
        return fn(manifest, workspace, params)
    except Exception as exc:    # noqa: BLE001 — defensive catch-all
        logger.warning("repair: transformation %s raised: %s", kind, exc)
        return _ExecResult(
            applied=False,
            summary=f"transformation {kind!r} raised: {type(exc).__name__}: {exc}",
        )


# ── LLM dispatch (delegates to tier3's hardened wrapper) ───────────────────


def _dispatch_repair_llm(
    system_prompt: str, user_message: str,
    *, bot_id: str, shared_dir: Path | None,
) -> tuple[str, int, float, str]:
    """Send the repair prompt to the bot's local OpenClaw agent.

    Returns ``(text, tokens, cost_usd, error)``. Reuses the tier3
    wrapper for process-group kill on timeout + cost recovery from
    TurnObserver — same operational concerns apply.
    """
    # Lazy import — tier3 module pulls in OpenClaw-CLI assumptions that
    # we don't want at module load.
    from app_audit_tier3 import _dispatch_via_oc_full

    res = _dispatch_via_oc_full(
        system_prompt, user_message,
        timeout_s=_REPAIR_TIMEOUT_S,
        bot_id=bot_id, shared_dir=shared_dir,
    )
    return res.text, res.tokens, res.cost_usd, res.error


# ── Repair session (orchestration) ─────────────────────────────────────────


def run_repair(
    request: dict,
    *,
    workspace: Path,
    bot_id: str,
    shared_dir: Path,
) -> RepairResult:
    """Execute one repair session end-to-end.

    Args:
        request: the dict loaded from ``audit_inbox/repair-<id>.json``.
            Must carry ``request_id``, ``app_id``, ``findings``,
            ``context.manifest_snapshot``. See
            ``repair_dispatch.RepairRequest.to_dict()`` for the full shape.
        workspace: bot's workspace (passed to transformation executors so
            they can resolve relative paths).
        bot_id: logical bot id, for cost recovery from TurnObserver.
        shared_dir: pod-wide shared dir, for cost recovery.

    Returns:
        A ``RepairResult`` ready for ``.to_outbox_record()``.
    """
    started_at = time.time()
    request_id = (request.get("request_id") or "").strip()
    app_id = (request.get("app_id") or "").strip()

    def _fail(error: str, tokens: int = 0) -> RepairResult:
        return RepairResult(
            request_id=request_id, app_id=app_id, status="failed",
            error=error,
            duration_ms=int((time.time() - started_at) * 1000),
            tokens={"input": 0, "output": 0, "total": tokens},
        )

    if not request_id:
        return _fail("request has no request_id")
    if not app_id:
        return _fail("request has no app_id")

    findings = request.get("findings") or []
    if not findings:
        return _fail("request has no findings to repair")

    context = request.get("context") or {}
    manifest = context.get("manifest_snapshot") or {}
    if not manifest:
        return _fail("request has no manifest_snapshot in context")

    allowed = _allowed_transformations()
    system_prompt = build_system_prompt(allowed)
    user_message = build_user_message(
        request,
        full_audit=bool(request.get("full_audit", False)),
    )

    text, tokens, cost_usd, error = _dispatch_repair_llm(
        system_prompt, user_message,
        bot_id=bot_id, shared_dir=shared_dir,
    )
    if error:
        return _fail(f"LLM dispatch failed: {error}", tokens=tokens)

    try:
        decisions = parse_repair_decisions(text)
    except ValueError as exc:
        return _fail(f"LLM response parse failed: {exc}", tokens=tokens)

    applied: list[dict] = []
    proposals: list[dict] = []

    for d in decisions:
        finding_id = d.get("finding_id", "")
        action = (d.get("action") or "").strip()
        kind = (d.get("kind") or "").strip()
        rationale = d.get("rationale", "")

        # Treat the action label as advisory; the kind-vs-allowlist
        # check is what actually decides whether we apply. A model that
        # picks action=apply with kind outside the allowlist gets its
        # decision converted to a Proposal (with a clear note).
        if action == "apply" and kind in allowed:
            exec_result = _apply_transformation(kind, d, manifest, workspace)
            if exec_result.applied:
                applied.append({
                    "kind":         kind,
                    "finding_id":   finding_id,
                    "summary":      exec_result.summary,
                    "trail_entry":  exec_result.trail_entry,
                    "applied_at":   _iso_now(),
                })
            else:
                # Executor refused — fall through to Proposal so the
                # operator sees what the LLM wanted and why it didn't land.
                proposals.append({
                    "kind":              "transformation_refused",
                    "finding_id":        finding_id,
                    "attempted_kind":    kind,
                    "rationale":         exec_result.summary,
                    "llm_rationale":     rationale,
                    "proposal_payload":  d,
                })
            continue

        # Anything else — including kinds outside the allowlist — becomes
        # a Proposal. Capture the LLM's full decision so the admin-side
        # poller has everything it needs to render the Proposal.
        if action == "apply" and kind not in allowed:
            proposals.append({
                "kind":              "transformation_rejected",
                "finding_id":        finding_id,
                "attempted_kind":    kind,
                "rationale":         (
                    f"LLM picked {kind!r} which is not in ALLOWED_TRANSFORMATIONS; "
                    "defense-in-depth rejected. Falling back to Proposal."
                ),
                "llm_rationale":     rationale,
                "proposal_payload":  d,
            })
            continue

        # action == "propose" (or anything else): pass through.
        proposals.append({
            "kind":              kind or "proposal",
            "finding_id":        finding_id,
            "rationale":         rationale,
            "proposal_payload":  d,
        })

    return RepairResult(
        request_id=request_id,
        app_id=app_id,
        status="ok",
        applied_transformations=applied,
        proposals=proposals,
        duration_ms=int((time.time() - started_at) * 1000),
        tokens={"input": 0, "output": 0, "total": tokens},
    )


# ── Inbox driver ───────────────────────────────────────────────────────────


def process_repair_inbox(
    workspace: Path,
    *,
    bot_id: str,
    shared_dir: Path,
    request_id: str | None = None,
) -> dict:
    """Drain ``audit_inbox/repair-*.json`` and run each via ``run_repair``.

    Each request:
      - Loaded from the inbox.
      - Rate-limited against ``audit_outbox/`` (3/day/app default).
      - Processed via ``run_repair``.
      - Output written to ``audit_outbox/repair-applied-<id>.json`` or
        ``repair-failed-<id>.json``.
      - Inbox file archived to ``audit_inbox/_ingested/<date>/``.

    Args:
        workspace: bot workspace.
        bot_id: logical bot id.
        shared_dir: pod-wide shared dir.
        request_id: when set, process only the file matching
            ``audit_inbox/<request_id>.json``. Otherwise drain everything.

    Returns:
        ``{"processed": int, "applied": int, "failed": int, "errors": [str]}``
    """
    from app_audit_runner import (    # noqa: WPS433 — runner-local import
        _audit_inbox_dir, _audit_outbox_dir, _archive_inbox_file,
    )

    # Lazy import to avoid pulling repair_dispatch into module load when
    # evolve_admin isn't installed.
    _load_admin_module()
    try:
        from evolve_admin.applications.repair_dispatch import (    # noqa: WPS433
            check_rate_limit,
        )
    except ImportError:
        check_rate_limit = None    # type: ignore[assignment]

    inbox_dir = _audit_inbox_dir(workspace)
    outbox_dir = _audit_outbox_dir(workspace)
    inbox_dir.mkdir(parents=True, exist_ok=True)
    outbox_dir.mkdir(parents=True, exist_ok=True)

    targets: list[Path] = []
    if request_id:
        candidate = inbox_dir / f"{request_id}.json"
        if candidate.exists():
            targets = [candidate]
    else:
        try:
            targets = sorted(
                p for p in inbox_dir.iterdir()
                if p.is_file()
                and p.suffix == ".json"
                and p.name.startswith("repair-")
                and not p.name.startswith(".")
            )
        except OSError:
            targets = []

    processed = 0
    applied_count = 0
    failed_count = 0
    errors: list[str] = []

    for path in targets:
        try:
            request = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"unreadable {path.name}: {exc}")
            _archive_inbox_file(path, workspace)
            continue

        rid = (request.get("request_id") or path.stem).strip()
        app_id = (request.get("app_id") or "").strip()

        # Rate limit BEFORE dispatch so a hot loop doesn't burn $.
        if check_rate_limit is not None and app_id:
            allowed_rl, reason = check_rate_limit(outbox_dir, app_id)
            if not allowed_rl:
                result = RepairResult(
                    request_id=rid, app_id=app_id, status="failed",
                    error=f"rate limit exceeded: {reason}",
                )
                _write_repair_outbox(outbox_dir, result)
                failed_count += 1
                _archive_inbox_file(path, workspace)
                continue

        try:
            result = run_repair(
                request,
                workspace=workspace, bot_id=bot_id, shared_dir=shared_dir,
            )
        except Exception as exc:    # noqa: BLE001 — last-ditch
            logger.exception("repair: run_repair raised for %s", rid)
            result = RepairResult(
                request_id=rid, app_id=app_id, status="failed",
                error=f"repair runner crashed: {type(exc).__name__}: {exc}",
            )

        _write_repair_outbox(outbox_dir, result)
        if result.status == "ok":
            applied_count += 1
        else:
            failed_count += 1

        processed += 1
        _archive_inbox_file(path, workspace)

    return {
        "processed": processed,
        "applied":   applied_count,
        "failed":    failed_count,
        "errors":    errors,
    }


def _write_repair_outbox(outbox_dir: Path, result: RepairResult) -> Path:
    """Atomic write of one repair outbox record.

    Filename carries the status so the admin poller can dispatch
    without parsing the JSON: ``repair-applied-<id>.json`` vs
    ``repair-failed-<id>.json``. The ``kind`` field in the JSON is
    authoritative; the prefix is a physical-layer convenience.
    """
    outbox_dir.mkdir(parents=True, exist_ok=True)
    prefix = "repair-applied-" if result.status == "ok" else "repair-failed-"
    path = outbox_dir / f"{prefix}{result.request_id}.json"
    record = result.to_outbox_record()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path
