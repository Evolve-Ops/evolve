"""
forge_engine.py — Unified Forge Engine for the App Gallery.

Orchestrates the full forge run cycle:
    Phase 1: Build    — LLM generates initial implementation
    Phase 2: Critique — 2-3 rounds of critic → builder refinement
    Phase 3: (Test step removed 2026-06-08 — app-test surface killed)
    Phase 4: Gate     — marks awaiting_approval, notifies operator
    Phase 5: Apply    — called on approval; finalises manifest and pkg_version

Entry points:
    run_forge_job(job_id, shared_dir, bot_id)          — bot agent session
    approve_forge_job(job_id, shared_dir, approved_by) — after operator approval

LLM calls go via urllib HTTPS POST to https://api.anthropic.com/v1/messages,
no third-party SDK dependency.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from ..config import bot_home as _bot_home

from . import bot_forge
from .forge_jobs import (
    ForgeJob,
    approve_job,
    complete_job,
    load_job,
    mark_awaiting_approval,
    mark_step_done,
    mark_step_failed,
    mark_step_running,
    save_job,
)
from .ids import next_pkg_version, now_iso
from .manifest import (
    ApplicationManifest,
    MANIFEST_SOURCE_GALLERY,
    MANIFEST_SOURCE_BOT_CREATED,
    MANIFEST_SOURCE_DISCOVERED,
    born_definition_status,
    ForgeCoherenceGateError,
    load_manifest,
    save_manifest,
    validate_coherence_gate,
    validate_delivery_contract,
)
# ForgeTestGateError + validate_test_gate removed 2026-06-08 — app-test
# surface killed per docs/decision-app-tests-2026-06-08.md.

log = logging.getLogger(__name__)


# ── Built-in LLM prompts ──────────────────────────────────────────────────────
#
# These are the defaults. Calibration overrides are loaded at runtime:
#   cal.get("prompts", "prompts.forge_builder.system") or BUILTIN_BUILDER_PROMPT

BUILTIN_BUILDER_PROMPT = (
    "You are a skilled software engineer building an app for an OpenClaw AI assistant bot. "
    "You will receive a build specification and the bot's workspace context. "
    "Your job is to write clean, working Python scripts and configuration files that implement "
    "the specification exactly. Follow the bot's established patterns. Handle edge cases. "
    "Write defensive code that works on first run with no existing data.\n\n"
    "OUTPUT FORMAT — strictly required:\n"
    "For every file you produce, use this exact structure:\n\n"
    "## FILE: relative/path/to/file.py\n"
    "```python\n"
    "# file content here\n"
    "```\n\n"
    "Rules:\n"
    "- The header must be exactly `## FILE: <relative-path>` on its own line\n"
    "- The path is relative to the bot's workspace root (no leading slash)\n"
    "- Follow immediately with a fenced code block using the correct language tag "
    "(python, json, sh, md, etc.)\n"
    "- Include ALL files in full — no truncation, no placeholders, no TODOs\n"
    "- Do NOT include provenance comment lines (# evolve: ...) — these are added automatically\n\n"
    "MULTI-APP CONTEXT — when the manifest declares `shared_modules` or `app_dependencies`:\n"
    "- Do NOT redefine modules owned by another app. Import them by exact name from the path "
    "declared in `shared_modules`.\n"
    "- If you own a shared module (your spec_id appears in `owned_by` and other specs appear in "
    "the module's `shared_with`), build it as a normal component with a clear public interface "
    "and the exports listed in dependents' `shared_modules.expected_exports`.\n"
    "- For shared state files (archives, indexes, opt-out registries), use atomic writes: "
    "temp-file + `os.replace`. Never partial writes.\n"
    "- If an expected import fails at runtime, surface a clear error and exit non-zero. Do not "
    "silently fall back to a stub.\n\n"
    "RECURSIVE LLM CONCERNS — when the manifest declares a `recursive_llm` block:\n"
    "- The app calls an LLM API internally. Implement exponential backoff with max 3 retries "
    "for transient errors (5xx, 429, timeouts).\n"
    "- If `fallback_required: true`, the app must function (degraded) when the LLM is "
    "unreachable. State your fallback strategy in code comments.\n"
    "- Log every LLM error with context (timestamp, error class, what was being done). Do not "
    "silently swallow.\n"
    "- Record per-call token counts and cost-USD for the audit roll-up. Read these from the API "
    "response.\n"
    "- Never infinite-retry. After exhausting retries, fail gracefully with a signal the verify "
    "daemon can pick up.\n\n"
    "AUTHORING GUIDE: see docs/manifest-authoring-guide.md for the calibrated contract this "
    "prompt expects manifests to satisfy. When updating this prompt, also update that guide "
    "(§1 enforces these defaults; §3.1 of docs/spec-export-import-forge-2026-05-26.md "
    "describes these multi-app + recursive-LLM additions)."
)

BUILTIN_EXTRACTOR_PROMPT = (
    "You are an interface documentation specialist. "
    "You will receive the final implementation of an app that was just built for an AI bot. "
    "Extract the stable external interface surfaces — the parts that other apps need to know "
    "about to integrate with this app. "
    "Return ONLY a JSON object (no explanation, no markdown fences) with this structure:\n"
    "{\n"
    '  "data_files": [\n'
    '    {"path": "relative/path.json", "description": "...",\n'
    '     "schema": {"storage_format": "...", "fields": {"field_name": "type and constraints"}}}\n'
    "  ],\n"
    '  "cli": [\n'
    '    {"command": "python3 scripts/foo.py <subcommand>",\n'
    '     "key_flags": ["--name NAME", "--status STATUS"],\n'
    '     "output_signals": ["SIGNAL_PREFIX:"]}\n'
    "  ],\n"
    '  "key_paths": {"descriptive_name": "relative/path"},\n'
    '  "enums": {"field_name": ["value1", "value2"]},\n'
    '  "terminal_states": ["complete", "cancelled"],\n'
    '  "signal_prefixes": ["FOLLOWUP_NEEDED:", "TASK_DUE:"]\n'
    "}\n\n"
    "Focus on: JSON file schemas with exact field names and types, CLI command signatures "
    "and flags, output signal prefixes used by cron/check scripts, enum values for "
    "status/type/state fields, and canonical file paths. "
    "Be precise — this JSON will be injected as context when other apps are built on top "
    "of this one. Omit any section that does not apply."
)

BUILTIN_CRITIC_PROMPT = (
    "You are a senior engineer reviewing an implementation before it ships. "
    "Your role is to advocate for the end user (the bot operator). "
    "Review the implementation and identify problems using these six lenses:\n"
    "1. COMPLETENESS: What's implied by the spec but not delivered? What's half-built?\n"
    "2. FIRST-RUN SAFETY: What breaks when no data exists yet? What fails on day one?\n"
    "3. LONGITUDINAL TRUST: What would the operator hit in the first week that would erode "
    "their confidence?\n"
    "4. EDGE CASES: What does the spec imply that the implementation ignores?\n"
    "5. SIMPLICITY: What is unnecessarily complex, fragile, or hard to debug?\n"
    "6. INTEGRATION: For apps that declare `shared_modules`, `app_dependencies`, or "
    "`recursive_llm` — are imports correct (exact names, expected paths)? Are atomic writes "
    "used for shared state? Does the app degrade gracefully when shared modules or LLM "
    "dependencies are unreachable? Are per-call cost telemetry fields populated for any "
    "LLM calls?\n\n"
    'Return a JSON array of issues: [{"category": "...", "description": "...", '
    '"severity": "blocking|should-fix|nice-to-have"}]\n'
    "Focus on blocking and should-fix issues. Be specific and actionable."
)

BUILTIN_RECONCILE_PROMPT = (
    "You are a technical writer updating a build specification to match what was actually built. "
    "You will receive the original build specification and the final implementation that was produced. "
    "Your task is to rewrite the build specification so it accurately describes what was built — "
    "including the actual file layout, data schemas, CLI interface, configuration, and any "
    "design decisions made during implementation. "
    "The result will be used as the specification for future improvement runs, so it must be "
    "accurate, complete, and match reality exactly. "
    "Write in the same style as the original spec: markdown, clear sections, concrete details. "
    "Return ONLY the updated specification text — no preamble, no explanation, no fences."
)

# Broken-config fallback for builder and critic models.
#
# Real selection goes through ``models.resolve_tier("tier2", config, bot_id)``
# — see _resolve_tier2_anthropic below. These literals only matter when:
#   (a) network.json is missing / unreadable, AND
#   (b) the analyzer package isn't importable (test isolation, broken paths)
# OR when tier2 resolves to a non-Anthropic provider (e.g. the operator's
# tier2 fallback chain hits openai/gpt-4o), which forge_engine can't dispatch
# because it calls the Anthropic Messages API directly via urllib.
#
# Operator override path (highest priority) is unchanged:
# network.json::forge.builder_model / network.json::forge.critic_model.
_DEFAULT_BUILDER_MODEL = "claude-sonnet-4-6"
_DEFAULT_CRITIC_MODEL  = "claude-sonnet-4-6"


# ── API key resolution ────────────────────────────────────────────────────────

def _resolve_api_key(bot_id: str | None = None) -> str:
    """
    Resolve the Anthropic API key from the environment or the bot's auth store.
    Returns an empty string if no key can be found (callers must handle gracefully).

    The SOURCE read is delegated to ``oc_store`` (per-agent sqlite store →
    legacy auth-profiles.json → transitional bak); the PARSER stays
    ``primary_bot.extract_anthropic_key``. When ``bot_id`` is given we hand
    ``oc_store`` the resolved home; otherwise we fall back to the current
    user's own ~/.openclaw store (the daemon-local path).
    """
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key

    from ..oc_store import read_anthropic_key

    if bot_id:
        k = read_anthropic_key(bot_id, home=_bot_home(bot_id))
        if k:
            return k
    # Daemon-local fallback: the invoking user's own ~/.openclaw store.
    return read_anthropic_key(None, home=Path(os.path.expanduser("~")))


# ── LLM call ─────────────────────────────────────────────────────────────────

def _call_anthropic(
    system_prompt: str,
    user_message: str,
    model: str,
    api_key: str,
    max_tokens: int = 8192,
) -> str:
    """
    Call the Anthropic Messages API directly via urllib (no SDK dependency).

    Returns the assistant's text response.
    Raises on HTTP errors or parse failures.
    """
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_message}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return data["content"][0]["text"]


# ── Logging helpers ───────────────────────────────────────────────────────────

def _log_dir(shared_dir: Path) -> Path:
    return shared_dir / "forge" / "logs"


def _log_path(job_id: str, shared_dir: Path) -> Path:
    return _log_dir(shared_dir) / f"{job_id}.log"


def _append_log(job_id: str, shared_dir: Path, msg: str) -> None:
    """Append a timestamped line to the job's log file."""
    log_file = _log_path(job_id, shared_dir)
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as f:
            f.write(f"[{now_iso()}] {msg}\n")
    except Exception as exc:
        log.warning("forge_engine: could not write to log %s: %s", log_file, exc)


# ── Pre-deploy C3 dispatch helper ──────────────────────────────────────────────

def _dispatch_c3_for_approval(
    *,
    job_id: str,
    shared_dir: Path,
    bot_id: str,
    app_id: str,
    manifest_dict: dict,
) -> None:
    """Dispatch Pass C3 LLM check before the forge approval gate fires.

    Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §6.5.

    Best-effort: any failure (missing module, no API key, network glitch,
    rate-limited) logs to the forge job log and returns. The gate that
    runs immediately after this call has Pass A to fall back on.

    Two pre-checks save the ~5k tokens C3 burns when we know the answer:

      * Already cached within 24h — ``is_rate_limited`` short-circuits.
      * Pass A structural status is ``incoherent`` — no point burning the
        LLM call on a manifest the gate is about to block on structural
        grounds. C3's job is to judge designs that hang together
        structurally but might not deliver the stated goal.
    """
    try:
        from .coherence_c3_dispatcher import dispatch_c3
        from .coherence_pass_c3 import is_rate_limited as _c3_rate_limited
        from .coherence_pass_a import run_pass_a, status_for_findings
    except Exception as exc:  # noqa: BLE001
        _append_log(
            job_id, shared_dir,
            f"C3 dispatch skipped: module load failed ({exc})",
        )
        return

    if _c3_rate_limited(manifest_dict):
        _append_log(
            job_id, shared_dir,
            "C3 dispatch skipped: cached verdict still within 24h window",
        )
        return

    # Pass A pre-gate: spend C3 tokens only when the manifest is
    # structurally worth judging. ``incoherent`` here means Pass A would
    # already block the approval gate — no operator-facing benefit to
    # also burning Sonnet/Haiku on top.
    try:
        a_findings = run_pass_a(manifest_dict)
        a_status = status_for_findings(a_findings)
    except Exception:  # noqa: BLE001
        # Pass A unavailable — proceed to C3 anyway; the gate ran fine
        # before C3 existed.
        a_status = "ok"
    if a_status == "incoherent":
        _append_log(
            job_id, shared_dir,
            "C3 dispatch skipped: Pass A status is incoherent — gate will "
            "block on structural grounds without needing C3",
        )
        return

    try:
        result = dispatch_c3(
            bot_id=bot_id, app_id=app_id, trigger="forge_approval",
            shared_dir=shared_dir,
        )
    except Exception as exc:  # noqa: BLE001
        _append_log(
            job_id, shared_dir,
            f"C3 dispatch raised (proceeding without C3 verdict): {exc}",
        )
        return

    if result.ok and result.check is not None:
        # Re-stamp the in-memory manifest_dict with the freshly-written
        # capability check so validate_coherence_gate (which runs against
        # this dict) reads the cached verdict immediately after dispatch.
        coherence = manifest_dict.setdefault("coherence", {})
        coherence["last_capability_check"] = result.check.to_dict()
        _append_log(
            job_id, shared_dir,
            f"C3 dispatch ok: severity={result.check.severity} "
            f"(model={result.model}, ~${result.cost_estimate_usd:.4f})",
        )
    elif result.skipped:
        _append_log(
            job_id, shared_dir,
            f"C3 dispatch skipped: {result.reason}",
        )
    else:
        _append_log(
            job_id, shared_dir,
            f"C3 dispatch failed: {result.error}",
        )


# ── Calibration helpers ───────────────────────────────────────────────────────

def _load_calibration(shared_dir: Path):
    """Return a CalibrationLoader, or None if calibration is unavailable."""
    try:
        from calibration import CalibrationLoader  # type: ignore[import]
        return CalibrationLoader(shared_dir)
    except Exception:
        return None


def _get_critique_rounds(manifest: ApplicationManifest, shared_dir: Path) -> int:
    """
    Return the number of critique rounds to run, respecting the priority order:
        per-app constraints.critique_rounds > calibration default > 2
    Maximum is 3 (spec § 6.3).
    """
    # Per-app override
    per_app = manifest.constraints.get("critique_rounds") if manifest.constraints else None
    if isinstance(per_app, int) and 1 <= per_app <= 3:
        return per_app

    # Calibration default
    cal = _load_calibration(shared_dir)
    if cal:
        try:
            val = cal.get("forge", "forge.critique_rounds.value", 2)
            if isinstance(val, int) and 1 <= val <= 3:
                return val
        except Exception:
            pass

    return 2


def _get_prompts(shared_dir: Path) -> tuple[str, str]:
    """Return (builder_system_prompt, critic_system_prompt) from calibration or built-ins."""
    cal = _load_calibration(shared_dir)
    builder_prompt = BUILTIN_BUILDER_PROMPT
    critic_prompt  = BUILTIN_CRITIC_PROMPT
    if cal:
        try:
            override = cal.get("prompts", "prompts.forge_builder.system")
            if override:
                builder_prompt = override
        except Exception:
            pass
        try:
            override = cal.get("prompts", "prompts.forge_critic.system")
            if override:
                critic_prompt = override
        except Exception:
            pass
    return builder_prompt, critic_prompt


def _resolve_tier2_anthropic(bot_id: str | None) -> str | None:
    """Resolve the pod's tier2 to a bare Anthropic model id (no provider prefix).

    forge_engine's ``_call_anthropic`` posts directly to the Anthropic
    Messages API, which accepts model ids like ``claude-sonnet-4-6`` —
    not the ``anthropic/<model>`` form Evolve's tier registry uses
    internally. We strip the prefix here.

    Returns ``None`` when:
      * the analyzer package isn't importable (test isolation, etc.)
      * network.json is missing / unreadable
      * tier2 resolves to a non-Anthropic provider (e.g. the operator's
        tier2 fallback chain hits openai/gpt-4o because Anthropic was
        flagged unhealthy). forge_engine can't dispatch those because
        it bypasses openclaw — callers fall back to the hardcoded
        default and the operator sees a warning in the admin log.

    Mirrors the resolver pattern used by applications.reviewer._resolve_tier3
    (which feeds an openclaw subprocess and so doesn't care about the
    provider prefix); the prefix stripping here is the only meaningful
    difference for this dispatch path.
    """
    try:
        from evolve_config import load_config  # type: ignore
        from models import resolve_tier  # type: ignore
        resolved = resolve_tier("tier2", load_config(), bot_id=bot_id)
    except Exception as exc:
        log.debug("forge_engine: tier2 resolve failed: %s", exc)
        return None
    if not resolved.startswith("anthropic/"):
        log.warning(
            "forge_engine: tier2 resolved to non-Anthropic %r — forge_engine "
            "calls the Anthropic API directly and cannot dispatch this. "
            "Falling back to %r. Configure network.json::forge.builder_model "
            "explicitly to silence this.",
            resolved, _DEFAULT_BUILDER_MODEL,
        )
        return None
    return resolved.split("/", 1)[1]


def _get_models(
    shared_dir: Path, bot_id: str | None = None,
) -> tuple[str, str]:
    """Return (builder_model, critic_model) for a forge job.

    Resolution priority, each step short-circuiting:
      1. network.json::forge.builder_model / forge.critic_model — explicit
         operator override. Stays whatever the operator wrote (no
         provider-prefix stripping; if they pinned ``anthropic/foo`` the
         API call will reject and they'll know to fix it).
      2. ``models.resolve_tier("tier2", config, bot_id=bot_id)`` — the pod's
         tier2 with per-bot ``tier_assignments`` override respected.
      3. ``_DEFAULT_BUILDER_MODEL`` / ``_DEFAULT_CRITIC_MODEL`` — hardcoded
         broken-config fallback.

    ``bot_id`` lets per-bot tier_assignments override a pod-wide tier2
    choice — useful when one bot's operator wants its forge runs on
    Opus while the rest of the pod uses Sonnet.
    """
    # Operator override (highest priority)
    operator_builder: str | None = None
    operator_critic: str | None = None
    try:
        network_path = (shared_dir / ".." / "network.json").resolve()
        if network_path.exists():
            cfg = json.loads(network_path.read_text())
            forge_cfg = cfg.get("forge", {})
            operator_builder = forge_cfg.get("builder_model") or None
            operator_critic  = forge_cfg.get("critic_model")  or None
    except Exception:
        pass

    # Tier2 resolution (per-bot override respected). Called once even when
    # both phases need it — resolve_tier is cheap but load_config does
    # disk I/O, no reason to do it twice.
    tier2 = (
        _resolve_tier2_anthropic(bot_id)
        if (operator_builder is None or operator_critic is None)
        else None
    )

    builder = operator_builder or tier2 or _DEFAULT_BUILDER_MODEL
    critic  = operator_critic  or tier2 or _DEFAULT_CRITIC_MODEL
    return builder, critic


def _provisioning_build_model(job: ForgeJob) -> str | None:
    """Model to pin this job's bot-driven build + refine dispatch to.

    Install jobs are app PROVISIONING (the wizard starter pack, a gallery
    install) — templated generation routed to the pod's ``standard`` role.
    Every other job_type (``improvement`` / ``update`` / ``hotfix``) refines
    EXISTING app code; that's steady-state work, left on the bot's default
    model so this never broadly downgrades non-provisioning builds. ``None``
    ⇒ the dispatch inherits ``agents.defaults.model`` (today's behavior).

    Resolution itself (and its fail-safe-to-None on broken config) lives in
    ``bot_forge._resolve_provisioning_build_model``; this is only the
    job_type scoping gate. Decision C —
    docs/finding-new-bot-activation-cost-2026-06-12.md.
    """
    if job.job_type != "install":
        return None
    return bot_forge._resolve_provisioning_build_model(job.bot_id)


def _remaining_provisioning_label(
    job: ForgeJob, shared_dir: Path,
) -> str:
    """A "N of M apps still to provision" label for the refusal Signal.

    Counts this bot's other install jobs still in the active dir
    (queued / running / awaiting_approval) — i.e. apps promised but not yet
    built — plus this refused one. Best-effort: any failure returns "" and the
    Signal simply omits the remaining-line. Pure read; never raises.
    """
    try:
        from .forge_jobs import list_active_jobs

        others = [
            j for j in list_active_jobs(shared_dir)
            if j.bot_id == job.bot_id
            and j.job_type == "install"
            and j.job_id != job.job_id
        ]
    except Exception:
        return ""
    # +1 for the job being refused right now.
    remaining = len(others) + 1
    return f"{remaining} app(s) not yet provisioned (incl. {job.app_id})"


def _provisioning_budget_decision(job: ForgeJob, shared_dir: Path):
    """Evaluate the provisioning budget for an install job, or None to allow.

    Returns a refusing ``ProvisioningBudgetDecision`` (already with its
    ``signal_payload`` emitted to the Signal store) when this provisioning
    build must be paused — the ceiling was reached or the daily cost breaker
    is tripped. Returns ``None`` when the dispatch is allowed (or when the
    budget module can't be evaluated — fail-open, so a backstop read failure
    never becomes a denial-of-service on provisioning).

    Only install jobs are provisioning; the caller gates on ``job_type``.
    The check runs at the app/job boundary BEFORE any manifest seed or build
    dispatch, so a refusal leaves nothing half-written — the sequential pack
    installer (queue_pack_installs) thereby gets clean complete-current-then-
    stop: the in-flight app finishes, the next is refused.
    """
    try:
        import importlib
        pb = importlib.import_module("provisioning_budget")
    except Exception as exc:
        log.debug("forge: provisioning_budget unavailable (%s); allowing", exc)
        return None

    try:
        decision = pb.evaluate(
            job.bot_id,
            shared_dir,
            kind=f"install build ({job.app_id})",
            job_id=job.job_id,
            remaining_label=_remaining_provisioning_label(job, shared_dir),
        )
    except Exception as exc:
        log.warning("forge: provisioning_budget.evaluate raised (%s); allowing", exc)
        return None

    if decision.allowed:
        return None

    # Emit the observable Signal from here (the load-bearing refusal is the
    # return value; the Signal is the alert + the half-provisioned-safety
    # proof that the standup was capped, not silently abandoned).
    if decision.signal_payload is not None:
        try:
            pb.emit_signal(shared_dir, decision.signal_payload)
        except Exception as exc:
            log.debug("forge: provisioning budget signal emit failed: %s", exc)
    return decision


def _derive_audit_eligibility(manifest: ApplicationManifest) -> bool:
    """Decide whether an app should be in the auto-audit pool.

    Heuristic (spec §6.1):
      - Ineligible when: no files OR only docs/data/state-layer files.
      - Eligible otherwise (default for anything with real code).

    Manual audits via CLI / UI / evo still run regardless.
    """
    code_extensions = (".py", ".sh", ".rb", ".js", ".ts", ".go", ".rs")
    files = manifest.files or []
    if not files:
        return False

    has_code = False
    only_docs_data = True
    for rec in files:
        if not isinstance(rec, dict):
            continue
        path = (rec.get("path") or "").lower()
        layer = rec.get("layer", "")
        if path.endswith(code_extensions):
            has_code = True
            only_docs_data = False
        elif layer not in ("data", "state", "reference") and not path.endswith(".md"):
            only_docs_data = False

    if not has_code and only_docs_data:
        return False
    return True


def _resolve_workspace_root(bot_id: str) -> Path:
    """
    Resolve the bot's workspace root directory.

    Reads openclaw.json → agents.defaults.workspace.
    Falls back to /Users/{bot_id}/.openclaw/workspace if not found.
    """
    try:
        oc_json = _bot_home(bot_id) / ".openclaw" / "openclaw.json"
        if oc_json.exists():
            oc_cfg = json.loads(oc_json.read_text(encoding="utf-8"))
            ws_str = oc_cfg.get("agents", {}).get("defaults", {}).get("workspace")
            if ws_str:
                return Path(ws_str)
    except Exception:
        pass
    return _bot_home(bot_id) / ".openclaw" / "workspace"


# ── Context package ───────────────────────────────────────────────────────────

def assemble_context_package(job: ForgeJob, shared_dir: Path) -> dict:
    """
    Build the context package passed to every LLM call in this forge run.

    Returns a dict with:
        manifest            — current manifest as dict (or skeleton for installs)
        build_spec          — from manifest
        improvement_history — manifest.improvement_history
        workspace_context   — contents of SOUL.md / AGENTS.md from bot workspace
        delta_spec          — for update jobs: from job.context_snapshot (if present)
    """
    manifest = load_manifest(job.app_id, job.bot_id, shared_dir)

    if manifest is None:
        # Install job with no manifest yet — provide a skeleton
        manifest_dict: dict = {
            "id": job.app_id,
            "bot_id": job.bot_id,
            "pkg_id": job.pkg_id,
            "gallery_version": job.gallery_version or "",
            "status": "updating",
        }
        build_spec = job.context_snapshot.get("build_spec", "")
        improvement_history: list = []
    else:
        manifest_dict = manifest.to_dict()
        # A v7-arc Instance carries no build_spec — the gallery package's
        # build_spec lives in the bound Spec and is snapshotted into the
        # job's context_snapshot at install time (gallery install route /
        # pack_driver). On a *re*-build the manifest already exists, so we
        # land here; without the context_snapshot fallback the LLM gets an
        # empty spec and the bot keeps its prior (possibly stale) files —
        # which is exactly why re-forging a v7-arc app could not pick up a
        # corrected Spec (e.g. the OC-2026.6 /api/message → openclaw-message-
        # send gallery migration). Prefer the instance's own build_spec when
        # it has one (v6 apps); fall back to the snapshotted package spec.
        build_spec = manifest.build_spec or job.context_snapshot.get("build_spec", "")
        improvement_history = manifest.improvement_history or []

    # Workspace context: look for SOUL.md and AGENTS.md in several locations
    workspace_context: dict[str, str] = {}
    soul_candidates = [
        Path(f"/Users/{job.bot_id}/SOUL.md"),
        Path(os.path.expanduser(f"~{job.bot_id}/SOUL.md")),
        Path(os.path.expanduser("~/.openclaw/SOUL.md")),
    ]
    agents_candidates = [
        Path(f"/Users/{job.bot_id}/AGENTS.md"),
        Path(os.path.expanduser(f"~{job.bot_id}/AGENTS.md")),
        Path(os.path.expanduser("~/.openclaw/AGENTS.md")),
    ]
    for p in soul_candidates:
        if p.exists():
            try:
                workspace_context["SOUL.md"] = p.read_text(encoding="utf-8")
            except Exception:
                pass
            break
    for p in agents_candidates:
        if p.exists():
            try:
                workspace_context["AGENTS.md"] = p.read_text(encoding="utf-8")
            except Exception:
                pass
            break

    ctx: dict = {
        "manifest": manifest_dict,
        "build_spec": build_spec,
        "improvement_history": improvement_history,
        "workspace_context": workspace_context,
    }

    # Update jobs: include delta_spec from the job's context_snapshot
    if job.job_type == "update":
        delta = job.context_snapshot.get("delta_spec")
        if delta:
            ctx["delta_spec"] = delta

    # ── Dependency context injection ──────────────────────────────────────────
    # For each app_dependency, load the installed manifest and inject:
    #   - interface_contract (if populated by forge)
    #   - key source files listed in interface_contract.key_paths (read from workspace)
    # This lets forge build integrations against the *actual* installed implementation
    # rather than abstract assumptions about what the dependency looks like.
    app_deps = manifest_dict.get("app_dependencies", [])
    if app_deps:
        dep_contexts: list[dict] = []
        from .manifest import list_manifests as _list_manifests
        # Build a pkg_id → installed manifest map for this bot
        installed_by_pkg: dict[str, ApplicationManifest] = {}
        try:
            for m in _list_manifests(shared_dir, job.bot_id):
                if m.pkg_id:
                    installed_by_pkg[m.pkg_id] = m
        except Exception:
            pass

        for dep in app_deps:
            dep_pkg_id = dep.get("pkg_id", "")
            dep_manifest = installed_by_pkg.get(dep_pkg_id)
            if dep_manifest is None:
                dep_contexts.append({
                    "pkg_id":       dep_pkg_id,
                    "display_name": dep.get("display_name", dep_pkg_id),
                    "state":        "not_installed",
                    "warning": (
                        f"Dependency {dep.get('display_name', dep_pkg_id)} "
                        f"({dep_pkg_id}) is not installed on bot {job.bot_id}. "
                        "Build against its specified interface_contract from the gallery manifest."
                    ),
                })
                continue

            dep_entry: dict = {
                "pkg_id":           dep_pkg_id,
                "display_name":     dep.get("display_name", dep_pkg_id),
                "state":            "installed",
                "pkg_version":      getattr(dep_manifest, "pkg_version", ""),
                "interface_contract": getattr(dep_manifest, "interface_contract", {}),
            }

            # Read key source files so forge sees the actual implementation
            contract = dep_entry["interface_contract"] or {}
            key_paths = contract.get("key_paths", {})
            if key_paths:
                source_files: dict[str, str] = {}
                ws_root = _resolve_workspace_root(job.bot_id)

                if ws_root:
                    for label, rel_path in key_paths.items():
                        try:
                            full_path = ws_root / rel_path
                            if full_path.exists() and full_path.stat().st_size < 64_000:
                                source_files[label] = full_path.read_text(encoding="utf-8")
                        except Exception:
                            pass
                if source_files:
                    dep_entry["source_files"] = source_files

            dep_contexts.append(dep_entry)

        if dep_contexts:
            ctx["dependency_context"] = dep_contexts

    return ctx


def _context_to_user_message(ctx: dict, phase: str = "build", critique: str | None = None) -> str:
    """
    Serialise the context package into a structured user message for the LLM.

    phase:   "build" | "refine" | "critique"
    critique: the critic's output (for refine phase)
    """
    parts: list[str] = []

    # Build spec — primary input
    if ctx.get("build_spec"):
        parts.append("## Build Specification\n\n" + ctx["build_spec"])

    # Workspace context
    wc = ctx.get("workspace_context", {})
    if wc.get("SOUL.md"):
        parts.append("## Bot Workspace Context (SOUL.md)\n\n" + wc["SOUL.md"])
    if wc.get("AGENTS.md"):
        parts.append("## Bot Workspace Context (AGENTS.md)\n\n" + wc["AGENTS.md"])

    # Delta spec for update jobs
    if ctx.get("delta_spec"):
        parts.append(
            "## Gallery Update Delta\n\n"
            + json.dumps(ctx["delta_spec"], indent=2)
        )

    # Improvement history (condensed for token efficiency)
    history = ctx.get("improvement_history", [])
    if history:
        summary_lines = []
        for entry in history[-5:]:  # last 5 runs
            summary_lines.append(
                f"- {entry.get('type', 'run')} {entry.get('run_id', '')} "
                f"({entry.get('pkg_version_after', '')}): "
                f"{entry.get('issues_found', 0)} issues found, "
                f"{entry.get('issues_resolved', 0)} resolved"
            )
        parts.append("## Prior Forge History (last 5 runs)\n\n" + "\n".join(summary_lines))

    # Current manifest state (condensed — just the operational fields)
    manifest = ctx.get("manifest", {})
    if manifest:
        condensed = {
            k: manifest[k]
            for k in ("id", "pkg_id", "pkg_version", "gallery_version", "status",
                       "files", "crons", "exported_hooks")
            if k in manifest
        }
        parts.append("## Current Manifest State\n\n" + json.dumps(condensed, indent=2))

    # Dependency context — installed dependency interfaces and source files
    dep_ctx = ctx.get("dependency_context", [])
    if dep_ctx:
        dep_parts: list[str] = []
        for dep in dep_ctx:
            name = dep.get("display_name", dep.get("pkg_id", "?"))
            state = dep.get("state", "unknown")
            if state != "installed":
                dep_parts.append(
                    f"### {name} ({dep.get('pkg_id', '')})\n"
                    f"**WARNING:** {dep.get('warning', 'Not installed — build defensively.')}"
                )
                continue
            dep_parts.append(f"### {name} — v{dep.get('pkg_version', '?')}")
            contract = dep.get("interface_contract")
            if contract:
                dep_parts.append(
                    "**Interface contract** (extracted by forge — authoritative):\n"
                    + json.dumps(contract, indent=2)
                )
            source_files = dep.get("source_files", {})
            for label, content in source_files.items():
                dep_parts.append(f"**{label}** (actual installed source):\n```\n{content}\n```")
        if dep_parts:
            parts.append(
                "## Installed Dependency Context\n\n"
                "The following apps are installed on this bot and this app integrates with them. "
                "Build integrations against the actual implementations shown — do NOT assume "
                "abstract interfaces. If source files are provided, use the exact field names, "
                "CLI flags, and file paths you see.\n\n"
                + "\n\n".join(dep_parts)
            )

    # Phase-specific instructions
    if phase == "build":
        parts.append(
            "## Task\n\n"
            "Produce a complete implementation based on the build specification above.\n\n"
            "Use the required output format: `## FILE: relative/path` header followed by "
            "a fenced code block for each file. Include all files in full. "
            "No placeholders, no TODOs, no truncation."
        )
    elif phase == "refine" and critique:
        parts.append(
            "## Critique to Address\n\n"
            + critique
            + "\n\n## Task\n\n"
            "Address all blocking and should-fix issues from the critique above. "
            "Produce a revised, complete implementation using the required "
            "`## FILE: relative/path` format for every file. "
            "Include all files in full — even unchanged ones. "
            "Briefly state which issues you addressed and how."
        )
    elif phase == "critique":
        parts.append(
            "## Task\n\n"
            "Review the implementation above against the build spec and return a JSON array "
            "of issues. Focus on blocking and should-fix items. Be specific and actionable."
        )

    return "\n\n---\n\n".join(parts)


# ── File materialisation ──────────────────────────────────────────────────────

def _parse_file_blocks(impl_text: str) -> list[tuple[str, str]]:
    """
    Parse ``## FILE: <path>`` blocks from LLM implementation output.

    Expected format::

        ## FILE: scripts/my_script.py
        ```python
        #!/usr/bin/env python3
        # content
        ```

    Returns a list of (relative_path, content) tuples in document order.
    Duplicate paths are deduplicated — last occurrence wins (refinement runs may
    restate files).
    """
    # Match: ## FILE: <path>\n```<lang?>\n<content>\n```
    pattern = re.compile(
        r'^##\s+FILE:\s+(\S+)\s*\n'   # header line: ## FILE: path
        r'```[^\n]*\n'                  # opening fence (with optional lang tag)
        r'(.*?)'                        # file content (non-greedy)
        r'^```\s*$',                    # closing fence on its own line
        re.MULTILINE | re.DOTALL,
    )
    seen: dict[str, str] = {}
    for m in pattern.finditer(impl_text):
        rel_path = m.group(1).strip().lstrip('/')
        content  = m.group(2)
        if rel_path:
            seen[rel_path] = content
    return list(seen.items())


# _parse_test_command + _parse_test_exemption_reason removed 2026-06-08 —
# app-test surface killed per docs/decision-app-tests-2026-06-08.md.


def _materialize_files(
    final_impl: str,
    workspace_root: Path,
    job: "ForgeJob",
    manifest: "ApplicationManifest",
    shared_dir: Path,
) -> list[str]:
    """
    Parse ``## FILE:`` blocks from *final_impl* and write them to *workspace_root*.

    For each file:
    1. Creates parent directories as needed.
    2. Writes the content.
    3. Stamps an ``evolve:`` provenance marker via ``provenance.embed_marker()``.
    4. Appends a v5 provenance record to ``manifest.files``.

    Returns the list of relative paths that were successfully written.
    Does not raise — failures are logged and skipped.
    """
    from .ids import new_file_id, next_file_version, now_iso as _now_iso
    try:
        from .provenance import embed_marker
    except Exception as exc:
        _append_log(job.job_id, shared_dir,
                    f"Materialize: could not import provenance.embed_marker — {exc}")
        embed_marker = None  # type: ignore[assignment]

    file_blocks = _parse_file_blocks(final_impl)
    if not file_blocks:
        _append_log(job.job_id, shared_dir,
                    "Materialize: no ## FILE: blocks found in implementation text")
        return []

    pkg_id       = job.pkg_id or ""
    pkg_version  = getattr(manifest, "pkg_version", "") or ""
    run_id       = job.run_id or ""
    ts           = _now_iso()

    # Build a lookup of existing file records keyed by path so we can
    # preserve file_id and bump file_version on re-writes.
    existing: dict[str, dict] = {}
    for rec in (manifest.files or []):
        if isinstance(rec, dict) and rec.get("path"):
            existing[rec["path"]] = rec

    written: list[str] = []
    new_file_records: list[dict] = []

    for rel_path, content in file_blocks:
        full_path = workspace_root / rel_path
        try:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
        except Exception as exc:
            _append_log(job.job_id, shared_dir,
                        f"Materialize: failed to write {rel_path}: {exc}")
            continue

        # Stamp provenance marker
        prev_rec      = existing.get(rel_path, {})
        file_id       = prev_rec.get("file_id") or new_file_id()
        prev_fv       = prev_rec.get("file_version")
        file_version  = next_file_version(prev_fv)

        if embed_marker is not None and pkg_id:
            try:
                embed_marker(
                    file_path     = full_path,
                    pkg_ids       = [pkg_id],
                    file_id       = file_id,
                    pkg_versions  = {pkg_id: pkg_version} if pkg_version else None,
                    file_version  = file_version,
                    merge         = False,  # forge owns this file outright
                )
            except Exception as exc:
                _append_log(job.job_id, shared_dir,
                            f"Materialize: embed_marker failed for {rel_path}: {exc}")

        # Build v5 provenance record
        new_file_records.append({
            "file_id":               file_id,
            "file_version":          file_version,
            "path":                  rel_path,
            "purpose":               "forge-generated",
            "owned_by":              job.app_id or "",
            "shared_with":           [],
            "created_in_run":        prev_rec.get("created_in_run") or run_id,
            "last_modified_in_run":  run_id,
            "created_at":            prev_rec.get("created_at") or ts,
            "modified_at":           ts,
        })
        written.append(rel_path)
        _append_log(job.job_id, shared_dir,
                    f"Materialize: wrote {rel_path} ({file_id}@{file_version})")

    # Merge: replace records for written paths, keep records for any pre-existing
    # paths that weren't in this run (e.g. shared data files not regenerated).
    written_set = set(written)
    kept = [r for r in (manifest.files or [])
            if isinstance(r, dict) and r.get("path") not in written_set]
    manifest.files = kept + new_file_records

    _append_log(job.job_id, shared_dir,
                f"Materialize: {len(written)} file(s) written to {workspace_root}")
    return written


# ── Phase implementations ─────────────────────────────────────────────────────

def _run_build_phase(
    job: ForgeJob,
    context: dict,
    shared_dir: Path,
    api_key: str,
    builder_model: str,
    builder_prompt: str,
) -> str:
    """
    Phase 1: initial build.
    Calls LLM with builder system prompt + context package.
    Returns the build output text.
    Marks step 2 running → done.
    """
    mark_step_running(job, 2, shared_dir)
    _append_log(job.job_id, shared_dir, "Phase 1: starting initial build")

    user_message = _context_to_user_message(context, phase="build")

    try:
        output = _call_anthropic(
            system_prompt=builder_prompt,
            user_message=user_message,
            model=builder_model,
            api_key=api_key,
            max_tokens=8192,
        )
    except Exception as exc:
        _append_log(job.job_id, shared_dir, f"Phase 1 LLM call failed: {exc}")
        # Retry once
        try:
            _append_log(job.job_id, shared_dir, "Phase 1: retrying LLM call")
            output = _call_anthropic(
                system_prompt=builder_prompt,
                user_message=user_message,
                model=builder_model,
                api_key=api_key,
                max_tokens=8192,
            )
        except Exception as exc2:
            err = f"Phase 1 build failed after retry: {exc2}"
            _append_log(job.job_id, shared_dir, err)
            mark_step_failed(job, 2, err, shared_dir)
            raise RuntimeError(err) from exc2

    _append_log(job.job_id, shared_dir, f"Phase 1: build complete ({len(output)} chars)")
    mark_step_done(job, 2, f"{len(output)} chars", shared_dir)
    return output


def _parse_critique_issues(critic_output: str) -> list[dict]:
    """
    Extract the JSON issues array from the critic's response.
    The critic is asked to return a JSON array; we find and parse it defensively.
    Returns a list of issue dicts, empty on parse failure.
    """
    # Look for a JSON array in the response
    start = critic_output.find("[")
    end   = critic_output.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        issues = json.loads(critic_output[start:end + 1])
        if isinstance(issues, list):
            return [i for i in issues if isinstance(i, dict)]
    except (json.JSONDecodeError, ValueError):
        pass
    return []


def _count_by_severity(issues: list[dict]) -> tuple[int, int, int]:
    """Return (blocking, should_fix, nice_to_have) counts."""
    blocking = sum(1 for i in issues if i.get("severity") == "blocking")
    should   = sum(1 for i in issues if i.get("severity") == "should-fix")
    nice     = sum(1 for i in issues if i.get("severity") == "nice-to-have")
    return blocking, should, nice


def _run_critique_cycle(
    job: ForgeJob,
    context: dict,
    build_output: str,
    shared_dir: Path,
    api_key: str,
    builder_model: str,
    critic_model: str,
    builder_prompt: str,
    critic_prompt: str,
    critique_rounds: int,
) -> str:
    """
    Phase 2: critique cycle.  Runs critique_rounds rounds of critic → refine.

    Each round uses steps (3, 4) for round 1, (5, 6) for round 2.
    Updates job.critique_rounds_done, job.issues_found, job.issues_resolved.
    Returns the final refined implementation text.
    """
    current_impl = build_output
    total_issues_found    = 0
    total_issues_resolved = 0
    total_deferred        = 0

    # Step numbers: critique steps are 3, 5, …; refine steps are 4, 6, …
    step_base = 3  # step_base = critique step, step_base+1 = refine step

    for round_num in range(1, critique_rounds + 1):
        critique_step = step_base + (round_num - 1) * 2
        refine_step   = critique_step + 1

        _append_log(job.job_id, shared_dir, f"Phase 2: critique round {round_num} starting")

        # ── Critique ──────────────────────────────────────────────────────────
        mark_step_running(job, critique_step, shared_dir)

        # Include the current implementation in the critique user message
        critique_context = dict(context)
        critique_user_msg = (
            _context_to_user_message(critique_context, phase="critique")
            + "\n\n---\n\n## Implementation to Review\n\n"
            + current_impl
        )

        critique_output = ""
        try:
            critique_output = _call_anthropic(
                system_prompt=critic_prompt,
                user_message=critique_user_msg,
                model=critic_model,
                api_key=api_key,
                max_tokens=4096,
            )
        except Exception as exc:
            _append_log(job.job_id, shared_dir,
                        f"Phase 2 round {round_num} critique LLM failed: {exc}")
            try:
                _append_log(job.job_id, shared_dir,
                            f"Phase 2 round {round_num} critique: retrying")
                critique_output = _call_anthropic(
                    system_prompt=critic_prompt,
                    user_message=critique_user_msg,
                    model=critic_model,
                    api_key=api_key,
                    max_tokens=4096,
                )
            except Exception as exc2:
                err = f"Phase 2 round {round_num} critique failed: {exc2}"
                _append_log(job.job_id, shared_dir, err)
                mark_step_failed(job, critique_step, err, shared_dir)
                raise RuntimeError(err) from exc2

        issues = _parse_critique_issues(critique_output)
        blocking, should, nice = _count_by_severity(issues)
        actionable = blocking + should
        total_issues_found += len(issues)

        detail = (
            f"Round {round_num}: {len(issues)} issues "
            f"({blocking} blocking, {should} should-fix, {nice} nice-to-have)"
        )
        _append_log(job.job_id, shared_dir, f"Phase 2 critique {detail}")
        job.issues_found = total_issues_found
        mark_step_done(job, critique_step, detail, shared_dir)

        # If no actionable issues, skip the refine step for this round
        if actionable == 0:
            _append_log(job.job_id, shared_dir,
                        f"Phase 2 round {round_num}: no blocking/should-fix issues, skipping refine")
            mark_step_running(job, refine_step, shared_dir)
            mark_step_done(job, refine_step, "No actionable issues — skipped", shared_dir)
            total_deferred += nice
            job.critique_rounds_done = round_num
            job.issues_deferred = total_deferred
            save_job(job, shared_dir)
            continue

        # ── Refine ────────────────────────────────────────────────────────────
        mark_step_running(job, refine_step, shared_dir)
        _append_log(job.job_id, shared_dir, f"Phase 2: builder refining (round {round_num})")

        refine_user_msg = (
            _context_to_user_message(context, phase="refine", critique=critique_output)
            + "\n\n---\n\n## Current Implementation\n\n"
            + current_impl
        )

        refined_output = ""
        try:
            refined_output = _call_anthropic(
                system_prompt=builder_prompt,
                user_message=refine_user_msg,
                model=builder_model,
                api_key=api_key,
                max_tokens=8192,
            )
        except Exception as exc:
            _append_log(job.job_id, shared_dir,
                        f"Phase 2 round {round_num} refine LLM failed: {exc}")
            try:
                _append_log(job.job_id, shared_dir,
                            f"Phase 2 round {round_num} refine: retrying")
                refined_output = _call_anthropic(
                    system_prompt=builder_prompt,
                    user_message=refine_user_msg,
                    model=builder_model,
                    api_key=api_key,
                    max_tokens=8192,
                )
            except Exception as exc2:
                err = f"Phase 2 round {round_num} refine failed: {exc2}"
                _append_log(job.job_id, shared_dir, err)
                mark_step_failed(job, refine_step, err, shared_dir)
                raise RuntimeError(err) from exc2

        current_impl = refined_output
        total_issues_resolved += actionable
        total_deferred += nice

        job.critique_rounds_done = round_num
        job.issues_resolved = total_issues_resolved
        job.issues_deferred = total_deferred

        refine_detail = (
            f"Round {round_num}: addressed {actionable} issues "
            f"({blocking} blocking, {should} should-fix); "
            f"{nice} deferred"
        )
        _append_log(job.job_id, shared_dir, f"Phase 2 refine {refine_detail}")
        mark_step_done(job, refine_step, refine_detail, shared_dir)
        save_job(job, shared_dir)

    _append_log(
        job.job_id, shared_dir,
        f"Phase 2 complete: {critique_rounds} rounds, "
        f"{total_issues_found} found, {total_issues_resolved} resolved, "
        f"{total_deferred} deferred",
    )
    return current_impl


def _run_test_phase(job: ForgeJob, shared_dir: Path) -> None:
    """
    Phase 3 NO-OP after 2026-06-08 — app-test surface killed per
    docs/decision-app-tests-2026-06-08.md. Step 7 is marked done immediately
    so the forge step counter stays consistent for existing callers; the
    step itself does nothing.
    """
    mark_step_running(job, 7, shared_dir)
    job.test_exit_code = None
    job.test_output_summary = "app-test surface removed 2026-06-08"
    _append_log(
        job.job_id, shared_dir,
        "Phase 3: app-test surface removed — step is a no-op",
    )
    mark_step_done(job, 7, "app-test surface removed", shared_dir)


# ── Integration check (Phase 3.5 — non-blocking) ──────────────────────────────
#
# Spec: docs/spec-export-import-forge-2026-05-26.md §3.3.
#
# For manifests declaring `shared_modules`, `app_dependencies`, or `recursive_llm`,
# verify that what the manifest promises is actually present in the workspace
# before approval. Findings are LOGGED, not gated — the operator sees them in
# the review panel but can still approve. This is signal, not a checkpoint.

def _ast_top_level_names(text: str) -> set[str]:
    """Return the set of names defined at module top level (defs, classes, assignments).

    Used to verify shared_modules' expected_exports without executing the file.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


def _check_shared_modules(shared_modules: list, workspace: Path) -> list[str]:
    """Verify each shared_module's file exists and exports the expected names."""
    issues: list[str] = []
    for sm in shared_modules:
        if not isinstance(sm, dict):
            issues.append(f"shared_modules entry not a dict: {sm!r}")
            continue
        name = sm.get("name", "")
        if not name:
            issues.append("shared_modules entry has no `name`")
            continue
        expected_exports = sm.get("expected_exports") or []
        module_path = workspace / "scripts" / (name.replace(".", "/") + ".py")
        if not module_path.exists():
            issues.append(
                f"shared_module {name!r}: expected file {module_path} not found "
                "(dependent apps will fail their imports)"
            )
            continue
        try:
            text = module_path.read_text(encoding="utf-8")
        except OSError as exc:
            issues.append(f"shared_module {name!r}: could not read {module_path}: {exc}")
            continue
        found = _ast_top_level_names(text)
        missing = [e for e in expected_exports if e not in found]
        if missing:
            issues.append(
                f"shared_module {name!r}: expected_exports {missing!r} not found at "
                f"module top level in {module_path}"
            )
    return issues


def _check_app_dependencies(app_dependencies: list, bot_id: str, shared_dir: Path) -> list[str]:
    """Verify each declared app_dependency has a manifest on the bot."""
    issues: list[str] = []
    apps_dir = shared_dir / "applications" / bot_id
    for dep in app_dependencies:
        if not isinstance(dep, dict):
            issues.append(f"app_dependencies entry not a dict: {dep!r}")
            continue
        dep_id = dep.get("spec_id") or dep.get("id", "")
        if not dep_id:
            issues.append("app_dependencies entry has no `spec_id`")
            continue
        manifest_path = apps_dir / f"{dep_id}.json"
        if not manifest_path.exists():
            issues.append(
                f"app_dependency {dep_id!r}: manifest not found at {manifest_path} "
                "(install the dependency first or remove the reference)"
            )
    return issues


def _check_recursive_llm(recursive_llm: dict, workspace: Path) -> list[str]:
    """Verify recursive_llm declarations point at real files + are complete."""
    issues: list[str] = []
    if not recursive_llm:
        return issues
    purposes = recursive_llm.get("purposes") or []
    if not purposes:
        issues.append("recursive_llm has no `purposes`; declare what the LLM is used for")
    api_key_source = recursive_llm.get("api_key_source", "")
    if api_key_source:
        full_path = workspace / api_key_source
        if not full_path.exists():
            issues.append(
                f"recursive_llm.api_key_source {api_key_source!r} → {full_path} not present; "
                "first run will need the operator to populate it"
            )
    if "fallback_required" not in recursive_llm:
        issues.append(
            "recursive_llm does not declare `fallback_required`; "
            "the builder cannot infer whether degraded operation is acceptable"
        )
    return issues


def _run_integration_check(job: ForgeJob, shared_dir: Path) -> None:
    """Non-blocking check between test (Step 7) and gate (Step 8).

    Only runs when the manifest declares any of: `shared_modules`,
    `app_dependencies`, `recursive_llm`. Findings are appended to the
    job's log; never raises, never blocks approval. The operator sees
    the findings in the review panel alongside test output.
    """
    manifest = load_manifest(job.app_id, job.bot_id, shared_dir)
    if not manifest:
        return

    raw = getattr(manifest, "raw", None) or {}
    shared_modules = raw.get("shared_modules") or []
    app_dependencies = raw.get("app_dependencies") or []
    recursive_llm = raw.get("recursive_llm") or {}

    if not (shared_modules or app_dependencies or recursive_llm):
        return  # Nothing declared; nothing to check.

    workspace = _bot_home(job.bot_id) / ".openclaw" / "workspace"
    if not workspace.exists():
        _append_log(
            job.job_id, shared_dir,
            f"Integration check: workspace {workspace} does not exist yet; skipping",
        )
        return

    try:
        issues: list[str] = []
        issues.extend(_check_shared_modules(shared_modules, workspace))
        issues.extend(_check_app_dependencies(app_dependencies, job.bot_id, shared_dir))
        issues.extend(_check_recursive_llm(recursive_llm, workspace))
    except Exception as exc:  # pragma: no cover — last-resort safety
        _append_log(
            job.job_id, shared_dir,
            f"Integration check: failed unexpectedly: {exc} (non-blocking; continuing)",
        )
        return

    if issues:
        for issue in issues:
            _append_log(job.job_id, shared_dir, f"Integration check: {issue}")
        _append_log(
            job.job_id, shared_dir,
            f"Integration check: {len(issues)} issue(s) found — non-blocking; "
            "operator can review and proceed or reject.",
        )
    else:
        _append_log(job.job_id, shared_dir, "Integration check: passed (no issues)")


# ── Phase 4.5 path ownership ──────────────────────────────────────────────────
#
# Phase 2 (bot build dispatch) writes workspace-relative build outputs.
# Phase 4.5 (``_materialize_scheduled_actions`` below) writes side-effect
# install artifacts — HEARTBEAT.md sections, LaunchAgent plists, etc. —
# from ``manifest.scheduled_actions[]``. The two domains never overlap.
#
# Real-world failure that motivated this split: a bot installs an app
# pre-mechanism-swap (e.g. task-manager pre-v17 with ``mechanism: launchd``,
# stamping ``installed_artifact: ~/Library/LaunchAgents/com.<bot>.task-check.plist``).
# The gallery republishes with ``mechanism: oc_heartbeat_instruction``. On the
# improvement run, the on-disk manifest still carries the stale launchd
# artifact, the bot echoes that path back in ``files_written``, and the
# Phase-2 verifier fails on "missing on disk" because v17 doesn't produce
# the plist. Phase 4.5 (which runs on approval) IS the source of truth for
# what was just installed; Phase-2 verification should defer Phase 4.5's
# domain to the post-apply A1 verifier.
# Spec: docs/spec-heartbeat-instruction-2026-06-03.md §4 (Phase 4.5
# ownership), §5 (A1 verifier).


def _phase45_owned_paths(
    manifest: ApplicationManifest | None,
    bot_id: str,
) -> set[str]:
    """Return paths that Phase 4.5 owns for this manifest's actions.

    Includes both the pre-build ``installed_artifact`` (possibly stale
    from a prior mechanism) and the path Phase 4.5 WILL install based on
    the current ``install`` recipe, so the filter catches both pre- and
    post-mechanism-swap manifestations.
    """
    owned: set[str] = set()
    if manifest is None:
        return owned

    actions = getattr(manifest, "scheduled_actions", None) or []
    if not isinstance(actions, list):
        return owned

    for action in actions:
        if not isinstance(action, dict):
            continue

        artifact = action.get("installed_artifact")
        if artifact:
            owned.add(str(artifact))

        mech = (action.get("mechanism") or "").strip()
        install_cfg = action.get("install") or {}
        if not isinstance(install_cfg, dict):
            continue

        if mech in ("oc_heartbeat_instruction", "oc_session_instruction"):
            file = (install_cfg.get("file") or "").strip()
            anchor = (install_cfg.get("section_anchor") or "").strip()
            if file:
                owned.add(file)
            if file and anchor:
                # Matches install_helpers._make_artifact: anchor with
                # leading "#"s + whitespace stripped.
                owned.add(f"{file}#{anchor.lstrip('#').strip()}")
        elif mech == "launchd":
            label = (install_cfg.get("plist_label") or "").strip()
            if label:
                # install_launch_agent writes the absolute form; stale
                # installed_artifact values may use the ~ form. Cover both.
                owned.add(f"/Users/{bot_id}/Library/LaunchAgents/{label}.plist")
                owned.add(f"~/Library/LaunchAgents/{label}.plist")
        elif mech == "launchd_python_signal":
            # v18: install_python_signal_action writes both a plist and a
            # wrapper script under the bot's workspace. Both are
            # Phase-4.5-owned and should be filtered out of bot-output
            # verification.
            label = (install_cfg.get("label") or "").strip()
            action_id = (action.get("id") or "").strip()
            if label:
                owned.add(f"/Users/{bot_id}/Library/LaunchAgents/{label}.plist")
                owned.add(f"~/Library/LaunchAgents/{label}.plist")
            if action_id:
                # Wrapper script path — relative form (as a manifest
                # might record it) AND absolute form (as the helper
                # writes it).
                rel = f"evolve/scheduled/{action_id}.py"
                owned.add(rel)
                owned.add(f"/Users/{bot_id}/.openclaw/workspace/{rel}")
        # crontab does not write to bot workspace — no path to filter.

    return owned


def _split_phase45_entries(
    files_written: list[dict],
    phase45_owned: set[str],
) -> tuple[list[dict], list[str]]:
    """Partition ``files_written`` into (bot-owned, phase-4.5-owned paths).

    The first element is the subset Phase-2 verification should check.
    The second is the list of dropped paths, for log messaging.
    """
    if not phase45_owned:
        return files_written, []
    bot_files: list[dict] = []
    filtered: list[str] = []
    for entry in files_written:
        path = (entry.get("path") or "").strip()
        if path and path in phase45_owned:
            filtered.append(path)
        else:
            bot_files.append(entry)
    return bot_files, filtered


# ── Bot-driven dispatch ───────────────────────────────────────────────────────

def _run_bot_dispatch(
    job: ForgeJob,
    context: dict,
    shared_dir: Path,
    critique_rounds: int,
) -> None:
    """Steps 2–7 of the forge run, executed by the target bot's LLM.

    The bot reads a request from ``workspace/evolve/forge/inbox/<job_id>.json``,
    builds the files (stamping provenance markers), runs the test command,
    and writes a summary to ``workspace/evolve/forge/outbox/<job_id>.json``.

    All admin server does here is dispatch the request, wait for the outbox,
    verify on-disk truth matches the summary, and roll the job's step state
    forward.

    Raises on dispatch / verify failure; the relevant step is marked failed
    before raising so the caller can return cleanly.
    """
    from .ids import now_iso as _now_iso

    # Step 1 (run_forge_job) is responsible for seeding the manifest from the
    # gallery package on fresh installs; by the time _run_bot_dispatch is
    # called, the manifest exists on disk (or Step 1 already failed the job).
    # If load_manifest still returns None here for an install job, treat it as
    # a fatal Step 2 failure — never proceed to a build dispatch without a
    # manifest, since Step 10 will reject the approval anyway.
    manifest = load_manifest(job.app_id, job.bot_id, shared_dir)
    if manifest is None and job.job_type == "install":
        err = (
            f"Step 2 prerequisite failed: manifest {job.app_id!r} not found for "
            f"bot {job.bot_id!r} — Step 1 should have seeded it from the gallery. "
            f"This indicates Step 1 was skipped or the manifest was deleted "
            f"between Step 1 and Step 2."
        )
        _append_log(job.job_id, shared_dir, err)
        mark_step_failed(job, 2, err, shared_dir)
        raise RuntimeError(err)

    manifest_dict: dict = manifest.to_dict() if manifest else {}
    build_spec = (manifest.build_spec if manifest else "") or \
                 context.get("build_spec", "")
    app_name = (manifest.name if manifest else "") or job.app_id

    # ── Step 2: files-pack fast path OR bot build dispatch ────────────────────
    # Spec: docs/spec-files-pack-hybrid-2026-06-03.md §7.
    # When the gallery package ships a files-pack AND the manifest's
    # files_pack field signals "use it", skip the LLM-driven build /
    # critique / refine cycle entirely. The fast path returns a
    # BuildResult-shaped object so the downstream verification +
    # manifest-records pipeline (and Phase 4.5 / Phase 5) run
    # unchanged.
    #
    # F-P.11.b adds a third path: partial files-pack. The dispatcher
    # stashes the install plan in ``job.context_snapshot['files_pack_partial']``;
    # we pass the bundled-path list to the LLM as paths_already_covered,
    # let the LLM build only the forge paths, then install the bundled
    # subset on top.
    mark_step_running(job, 2, shared_dir)

    files_pack_result = _maybe_install_via_files_pack(
        job, manifest, shared_dir,
    )
    partial_plan: dict = job.context_snapshot.get("files_pack_partial") or {}

    # Build the request now, after the dispatcher has had a chance to
    # decide; this lets us slot in paths_already_covered for partial mode.
    request = bot_forge.BuildRequest(
        job_id=job.job_id,
        kind="build",
        pkg_id=job.pkg_id or "",
        pkg_version=(manifest.pkg_version if manifest else "") or "",
        app_id=job.app_id or "",
        app_name=app_name,
        build_spec=build_spec,
        manifest=manifest_dict,
        instructions="",
        paths_already_covered=sorted(partial_plan.get("bundled_paths") or []),
        # Set by /api/forge/jobs/<id>/retry — tells the bot's LLM to wipe
        # ``apps/<app_id>/`` before re-building so leftover files from
        # the prior attempt don't survive into the fresh install.
        # Defaults False via getattr so older serialised jobs
        # missing the field round-trip safely.
        #
        # Scoped to install jobs only: improvement/update flows refine
        # EXISTING app code, and a blanket rm -rf would clobber prior
        # work. The reported bug was an install retry — that's the only
        # path where wiping apps/ is clearly correct.
        is_retry=bool(
            getattr(job, "is_retry", False)
            and job.job_type == "install"
        ),
    )

    # Provisioning builds (fresh-app installs) are templated generation that
    # doesn't need the power rung — pin build + refine to the pod's standard
    # role. Steady-state forge work (improvement / update / hotfix) keeps the
    # bot's default, so this never broadly downgrades non-provisioning builds.
    # None ⇒ inherit the bot's agent default (today's behavior). Resolved once
    # and reused for the build dispatch below and every refine round.
    provisioning_build_model = _provisioning_build_model(job)

    if files_pack_result is not None:
        # Complete coverage — every file came from the pack.
        result = files_pack_result
        # Skip critique rounds — the files came from a previously-
        # critiqued snapshot; no LLM judgment to add here.
        job.context_snapshot["files_pack_install"] = True
    else:
        _append_log(job.job_id, shared_dir,
                    f"Bot dispatch: kind=build bot={job.bot_id} pkg={job.pkg_id} "
                    f"timeout=1200s "
                    f"paths_already_covered={len(request.paths_already_covered)}")
        try:
            result = bot_forge.dispatch_build(
                job.bot_id, request, timeout_sec=1200,
                model=provisioning_build_model,
            )
        except Exception as exc:
            err = f"Bot build dispatch failed: {exc}"
            _append_log(job.job_id, shared_dir, err)
            mark_step_failed(job, 2, err, shared_dir)
            raise

        # Partial files-pack: now that the LLM-built forge files exist,
        # install the bundled subset on top. Bundled-content wins over
        # anything the LLM happened to write at those paths (safety net
        # against the LLM ignoring paths_already_covered).
        if partial_plan and result.status in ("complete", "ok", "success"):
            partial_files_written = _install_partial_files_pack(
                job, partial_plan, shared_dir,
            )
            if partial_files_written:
                # Merge bundled entries into the build result so the
                # downstream verify / manifest-records pipeline sees
                # the full file set. Deduplicate on path — bundled
                # entries replace any LLM-written stub at the same path.
                bundled_paths_set = {f["path"] for f in partial_files_written}
                merged = [
                    e for e in (result.files_written or [])
                    if e.get("path") not in bundled_paths_set
                ]
                merged.extend(partial_files_written)
                result.files_written = merged
                job.context_snapshot["files_pack_install"] = "partial"

    _append_log(
        job.job_id, shared_dir,
        f"Bot dispatch: status={result.status!r} files={len(result.files_written)} "
        f"test_exit={result.test_exit_code} agent_rc={result.agent_exit_code}",
    )

    if result.status not in ("complete", "ok", "success"):
        err = (
            f"Bot reported status={result.status!r}: "
            f"{(result.notes or 'no notes')[:200]}"
        )
        _append_log(job.job_id, shared_dir, err)
        mark_step_failed(job, 2, err, shared_dir)
        raise RuntimeError(err)

    # ── Verify files on disk ─────────────────────────────────────────────────
    # Drop any Phase 4.5-owned entries the bot may have echoed back (e.g. a
    # stale ``installed_artifact`` from a pre-mechanism-swap install). Phase
    # 4.5 / the post-apply A1 verifier owns those.
    phase45_owned = _phase45_owned_paths(manifest, job.bot_id)
    bot_files, phase45_filtered = _split_phase45_entries(
        result.files_written, phase45_owned,
    )
    if phase45_filtered:
        _append_log(
            job.job_id, shared_dir,
            f"Bot output: skipping {len(phase45_filtered)} Phase 4.5-owned "
            f"entry(ies) (verification deferred to A1): {phase45_filtered[:3]}",
        )

    # Evolve is the hash authority: the verifier recomputes each file's
    # sha256 from disk and returns records carrying the recomputed hash.
    # The bot's claimed sha256 is advisory — a placeholder or a stale
    # hash-then-edit claim logs a warning, never fails the build. This
    # also covers recovery: when `recover_orphaned_jobs` replays step 2
    # after a refine round already rewrote the same paths, the stale
    # build-outbox claims warn instead of failing.
    verified, hash_warnings, errors = bot_forge.verify_files_on_disk(
        job.bot_id, bot_files,
    )
    for warn in hash_warnings:
        _append_log(job.job_id, shared_dir, f"Step 2 verify advisory: {warn}")
    if errors:
        err = f"Bot output verification failed: {'; '.join(errors)[:300]}"
        _append_log(job.job_id, shared_dir, err)
        mark_step_failed(job, 2, err, shared_dir)
        raise RuntimeError(err)

    mark_step_done(
        job, 2, f"Bot built {len(verified)} file(s)", shared_dir
    )

    # ── Update manifest.files with the bot's provenance records ──────────────
    # ``manifest.files`` records workspace build outputs; Phase 4.5 artifacts
    # live in ``scheduled_actions[].installed_artifact`` instead. Use the
    # verified records (filtered + evolve-recomputed sha256) so a stale
    # echoed plist path can't leak into ``manifest.files`` and the recorded
    # hashes describe actual disk content.
    if manifest is not None:
        records = bot_forge.build_manifest_file_records(
            bot_id=job.bot_id,
            files_written=verified,
            app_id=job.app_id or "",
            pkg_id=job.pkg_id or manifest.pkg_id or "",
            run_id=job.run_id or "",
            now_iso_str=_now_iso(),
            existing=manifest.files or [],
        )
        # Replace records for written paths, keep unrelated pre-existing.
        written_paths = {(e.get("path") or "").lstrip("/")
                         for e in verified}
        kept = [
            r for r in (manifest.files or [])
            if isinstance(r, dict)
            and (r.get("path") or "").lstrip("/") not in written_paths
        ]
        manifest.files = kept + records
        try:
            save_manifest(manifest, shared_dir)
        except Exception as exc:
            _append_log(
                job.job_id, shared_dir,
                f"Could not save updated manifest.files (non-fatal): {exc}",
            )

    # ── Steps 3–6: critique cycle (bot-driven self-review) ───────────────────
    # Maps onto the existing step layout:
    #   3: Critique round 1
    #   4: Refine round 1   (skipped if round 1 found no blockers)
    #   5: Critique round 2 (skipped if round 1 had no blockers — same)
    #   6: Refine round 2   (final)
    #
    # Each round: bot reviews its own files, returns issues. If no blockers,
    # we exit the loop early (marking remaining steps "skipped — clean") so
    # the progress bar reads truthfully. If blockers exist, dispatch a refine
    # request — bot rewrites the offending files and re-runs tests.
    success_criteria = (manifest.success_criteria if manifest else {}) or {}
    critique_step_pairs = [(3, 4), (5, 6)]  # (critique, refine) per round
    # Track the current set of files for critique review (refreshed after refines).
    # Critique reviews the bot's own work — Phase 4.5 artifacts are reviewed by
    # the A1 verifier, not by the bot's self-critique.
    current_files = list(verified)
    previous_test_output = result.test_output

    # Files-pack fast-path: the files came from a previously-critiqued
    # snapshot; there's nothing new for the bot's self-critique to
    # add. Mark every critique + refine step as skipped-with-reason
    # so the operator-review UI shows the explicit cause.
    skip_critique_for_files_pack = bool(
        job.context_snapshot.get("files_pack_install")
    )

    for round_idx, (critique_step, refine_step) in enumerate(critique_step_pairs):
        round_num = round_idx + 1
        if skip_critique_for_files_pack:
            for step in (critique_step, refine_step):
                mark_step_running(job, step, shared_dir)
                mark_step_done(
                    job, step,
                    "Skipped — files-pack install (no LLM-generated "
                    "content to critique)",
                    shared_dir,
                )
            continue
        if round_num > critique_rounds:
            # Round configured below total; mark remaining as skipped-by-config
            for step in (critique_step, refine_step):
                mark_step_running(job, step, shared_dir)
                mark_step_done(
                    job, step,
                    f"Skipped — only {critique_rounds} round(s) configured",
                    shared_dir,
                )
            continue

        # ── Critique step ─────────────────────────────────────────────────
        mark_step_running(job, critique_step, shared_dir)
        crit_request = bot_forge.CritiqueRequest(
            job_id=job.job_id,
            round=round_num,
            pkg_id=job.pkg_id or "",
            app_id=job.app_id or "",
            build_spec=build_spec,
            files_to_review=[
                {"path": (e.get("path") or "").lstrip("/"),
                 "file_id": e.get("file_id", "")}
                for e in current_files
                if (e.get("path") or "").strip()
            ],
            success_criteria=success_criteria,
            previous_test_output=previous_test_output or "",
            manifest=manifest.to_dict() if manifest else {},
        )
        _append_log(
            job.job_id, shared_dir,
            f"Critique round {round_num} dispatch: bot={job.bot_id} "
            f"files_to_review={len(crit_request.files_to_review)}",
        )

        try:
            crit_result = bot_forge.dispatch_critique(
                job.bot_id, crit_request, timeout_sec=600,
            )
        except Exception as exc:
            # Critique failure is non-fatal — we proceed to approval without
            # this round's feedback. Mark step done with a warning detail.
            err = f"Critique round {round_num} failed (non-fatal): {exc}"
            _append_log(job.job_id, shared_dir, err)
            mark_step_done(job, critique_step, f"Failed: {str(exc)[:80]}", shared_dir)
            mark_step_running(job, refine_step, shared_dir)
            mark_step_done(
                job, refine_step,
                "Skipped — critique failed; nothing to refine",
                shared_dir,
            )
            continue

        n_blocking = len(crit_result.blocking_issues)
        n_should = len(crit_result.should_fix_issues)
        n_total = len(crit_result.issues)
        job.issues_found = (job.issues_found or 0) + n_total
        critique_summary = (
            f"{n_total} issue(s): {n_blocking} blocking, {n_should} should-fix"
        )
        _append_log(
            job.job_id, shared_dir,
            f"Critique round {round_num}: {critique_summary}",
        )
        mark_step_done(job, critique_step, critique_summary, shared_dir)

        # ── Refine step (only if there are actionable issues) ─────────────
        actionable = crit_result.blocking_issues + crit_result.should_fix_issues
        if not actionable:
            mark_step_running(job, refine_step, shared_dir)
            mark_step_done(job, refine_step, "No actionable issues — skipped", shared_dir)
            _append_log(
                job.job_id, shared_dir,
                f"Refine round {round_num} skipped: critique returned no "
                f"blocking or should-fix issues",
            )
            # If no blockers in the FIRST round, we can short-circuit the
            # second round entirely — nothing has changed since critique 1.
            if round_idx == 0:
                for step in (5, 6):
                    mark_step_running(job, step, shared_dir)
                    mark_step_done(
                        job, step,
                        "Skipped — round 1 found no actionable issues",
                        shared_dir,
                    )
                break
            continue

        # Actionable issues → dispatch refine
        mark_step_running(job, refine_step, shared_dir)
        refine_request = bot_forge.RefineRequest(
            job_id=job.job_id,
            round=round_num,
            pkg_id=job.pkg_id or "",
            app_id=job.app_id or "",
            build_spec=build_spec,
            files_in_workspace=crit_request.files_to_review,
            issues_to_address=actionable,
            manifest=manifest.to_dict() if manifest else {},
        )
        _append_log(
            job.job_id, shared_dir,
            f"Refine round {round_num} dispatch: addressing "
            f"{n_blocking} blocking + {n_should} should-fix issue(s)",
        )

        try:
            refine_result = bot_forge.dispatch_refine(
                job.bot_id, refine_request, timeout_sec=1200,
                model=provisioning_build_model,
            )
        except Exception as exc:
            err = f"Refine round {round_num} failed: {exc}"
            _append_log(job.job_id, shared_dir, err)
            mark_step_failed(job, refine_step, err, shared_dir)
            raise

        if refine_result.status not in ("complete", "ok", "success"):
            err = (
                f"Refine round {round_num} reported status="
                f"{refine_result.status!r}: {(refine_result.notes or '')[:200]}"
            )
            _append_log(job.job_id, shared_dir, err)
            mark_step_failed(job, refine_step, err, shared_dir)
            raise RuntimeError(err)

        # Verify the refined files exist (the bot may have touched a subset).
        # Same Phase 4.5 filtering applied here — a refine round can re-echo
        # the same stale install artifact that the build round did.
        refine_bot_files, refine_filtered = _split_phase45_entries(
            refine_result.files_written,
            _phase45_owned_paths(manifest, job.bot_id),
        )
        if refine_filtered:
            _append_log(
                job.job_id, shared_dir,
                f"Refine round {round_num}: skipping {len(refine_filtered)} "
                f"Phase 4.5-owned entry(ies): {refine_filtered[:3]}",
            )
        # Same evolve-side hash authority as the build verify above: the
        # refine's claims are advisory, disk is recomputed, and recovery
        # replays (refine round 1 after round 2 touched the same files)
        # warn instead of failing.
        verified, hash_warnings, errors = bot_forge.verify_files_on_disk(
            job.bot_id, refine_bot_files,
        )
        for warn in hash_warnings:
            _append_log(
                job.job_id, shared_dir,
                f"Refine round {round_num} verify advisory: {warn}",
            )
        if errors:
            err = (
                f"Refine round {round_num} verification failed: "
                f"{'; '.join(errors)[:300]}"
            )
            _append_log(job.job_id, shared_dir, err)
            mark_step_failed(job, refine_step, err, shared_dir)
            raise RuntimeError(err)

        job.issues_resolved = (job.issues_resolved or 0) + n_blocking + n_should
        job.critique_rounds_done = round_num
        refine_summary = (
            f"Refined {len(verified)} file(s); test exit "
            f"{refine_result.test_exit_code}"
        )
        _append_log(job.job_id, shared_dir,
                    f"Refine round {round_num}: {refine_summary}")
        mark_step_done(job, refine_step, refine_summary, shared_dir)

        # ── Update manifest.files records with refined file metadata ──────
        # Use the verified records (Phase-4.5-filtered + evolve-recomputed
        # sha256), same reason as the build path above: ``manifest.files``
        # records workspace build outputs only, with authoritative hashes.
        if manifest is not None and verified:
            refined_paths = {
                (e.get("path") or "").lstrip("/")
                for e in verified
            }
            kept = [
                r for r in (manifest.files or [])
                if isinstance(r, dict)
                and (r.get("path") or "").lstrip("/") not in refined_paths
            ]
            refined_records = bot_forge.build_manifest_file_records(
                bot_id=job.bot_id,
                files_written=verified,
                app_id=job.app_id or "",
                pkg_id=job.pkg_id or manifest.pkg_id or "",
                run_id=job.run_id or "",
                now_iso_str=_now_iso(),
                existing=manifest.files or [],
            )
            manifest.files = kept + refined_records
            try:
                save_manifest(manifest, shared_dir)
            except Exception as exc:
                _append_log(
                    job.job_id, shared_dir,
                    f"Could not save refined manifest.files (non-fatal): {exc}",
                )

        # Refresh the file set + test output for the next round's critique
        current_files = verified or current_files
        previous_test_output = refine_result.test_output or previous_test_output
        # Carry the refine's test result forward — Phase 3 uses the latest
        result.test_run = refine_result.test_run or result.test_run
        result.test_exit_code = (
            refine_result.test_exit_code
            if refine_result.test_exit_code is not None
            else result.test_exit_code
        )
        result.test_output = refine_result.test_output or result.test_output

    # ── Phase 2.5: Static analysis (spec-forge-side-effects §13) ─────────────
    # Run three pure checks before the test gate finalises so the operator
    # review surface can show what the LLM critique cycle missed:
    #
    #   §13.4 orphan_check        — top-level functions with no call sites
    #                                (catches the ea_config smoking gun)
    #   §13.2 constraint_critic   — for each constraints.boundaries[],
    #                                constraints.safety[], identity.scope_includes[]
    #                                item: does the code implement it? LLM verdict.
    #   §13.3 negative_path_tests — regex-derived shell assertions for "X when Y"
    #                                shaped constraints (gateway-unreachable,
    #                                configurable-via-bot-config). MVP: skeletons
    #                                go onto context_snapshot for operator review;
    #                                test_command augmentation is a follow-up.
    #
    # Findings are advisory in PR 6 — they're stamped on the job and logged
    # but do NOT (yet) block approval or auto-refine. The operator sees them
    # at the approval gate. Future PR gates on `absent` constraint findings.
    try:
        sa_summary = _run_static_analysis_phase(
            job, manifest, shared_dir,
            api_key=api_key, critic_model=critic_model,
            current_files=current_files or [],
        )
        job.context_snapshot["static_analysis_findings"] = sa_summary
        _append_log(
            job.job_id, shared_dir,
            "Phase 2.5: static analysis — "
            f"orphans={sa_summary['orphan_count']}, "
            f"constraint_absent={sa_summary['constraint_absent_count']}, "
            f"negative_path_tests={sa_summary['negative_path_test_count']}, "
            f"env_portability={sa_summary['env_portability_count']}",
        )
        if (sa_summary["constraint_absent_count"]
            or sa_summary["orphan_count"]
            or sa_summary["env_portability_count"]):
            _append_log(
                job.job_id, shared_dir,
                "Phase 2.5: ADVISORY findings present — operator review at "
                "approval gate (gating is a follow-up PR)",
            )
    except Exception as exc:
        # Belt-and-suspenders: the helper catches per-check errors, so a
        # top-level exception here is structural. Log + continue — Phase 3
        # (test gate) should still run.
        _append_log(
            job.job_id, shared_dir,
            f"Phase 2.5: static analysis skipped (non-fatal): {exc}",
        )

    # ── Step 7: app-test surface removed 2026-06-08 (no-op) ──────────────────
    # Was: record test result from the bot-side outbox. Now a no-op so the
    # forge step counter stays consistent for existing callers.
    mark_step_running(job, 7, shared_dir)
    job.test_exit_code = None
    job.test_output_summary = "app-test surface removed 2026-06-08"
    mark_step_done(job, 7, "app-test surface removed", shared_dir)
    _append_log(
        job.job_id, shared_dir,
        "Phase 3 (bot-run test): no-op (app-test surface removed)",
    )

    save_job(job, shared_dir)


# ── Notification ──────────────────────────────────────────────────────────────

def _notify_operator(job: ForgeJob, shared_dir: Path) -> None:
    """
    Attempt to notify the operator that a forge job is awaiting approval.

    Routes through the alerts dispatcher (Phase 3f of the alert-notifier
    spec). On any non-SENT outcome — operator-disabled, no recipient
    configured, dispatch failure — falls back to writing a notification
    file in the log dir so the job is still discoverable.

    Operator-tunable enable/cooldown via
    ``alerts.forge_engine.{enabled, cooldown_seconds}``. dedup_key is
    per-job since each job is a unique operator-relevant event.
    """
    # Build the on-disk fallback body locally — keeps the richer
    # operator-discoverable text even when chat delivery is muted /
    # fails. The chat push uses the catalog's compact format via the
    # payload below.
    fallback_msg = (
        f"Forge job ready for review\n"
        f"App: {job.app_id} ({job.pkg_id})\n"
        f"Bot: {job.bot_id}\n"
        f"Job: {job.job_id}\n"
        f"Type: {job.job_type}\n"
        f"Critique: {job.critique_rounds_done} round(s), "
        f"{job.issues_found} issues found, {job.issues_resolved} resolved\n"
        f"Test: exit {job.test_exit_code if job.test_exit_code is not None else 'n/a'}\n"
        f"To approve: tell {job.bot_id} 'approve {job.app_id}'"
    )

    # Load network config (used for recipient resolution by the dispatcher).
    network: dict = {}
    network_path = (shared_dir / ".." / "network.json")
    try:
        resolved = network_path.resolve()
        if resolved.exists():
            network = json.loads(resolved.read_text())
    except Exception:
        pass

    # Route through the dispatcher. Phase F: payload-driven; catalog
    # body_template renders "📋 Forge job ready for review / App: {app_id}
    # ({pkg_id}) / Bot: {bot_id}  Type: {job_type}" plus a ui_action
    # pointing the operator at the right Forge page. On any non-SENT
    # outcome we fall back to a discoverable on-disk notification file
    # so the job is not lost.
    notified = False
    try:
        from ..alerts.dispatcher import (
            send as _dispatch_send, DispatchResult, Severity,
        )
        outcome = _dispatch_send(
            shared_dir=shared_dir,
            network=network,
            source="forge_engine",
            severity=Severity.INFO,
            dedup_key=f"forge/{job.job_id}",
            catalog_event="decisions.forge_job_ready",
            payload={
                "app_id": job.app_id,
                "pkg_id": job.pkg_id,
                "bot_id": job.bot_id,
                "job_type": job.job_type,
            },
        )
        if outcome.result == DispatchResult.SENT:
            notified = True
            _append_log(
                job.job_id, shared_dir,
                f"Operator notified via {outcome.channel}",
            )
        else:
            _append_log(
                job.job_id, shared_dir,
                f"Dispatcher did not deliver ({outcome.result.value}); "
                f"falling back to on-disk notification",
            )
    except Exception as exc:
        _append_log(
            job.job_id, shared_dir,
            f"Could not send operator notification via dispatcher: {exc}",
        )

    if not notified:
        # Fallback: write a notification file the operator can discover
        notify_path = _log_dir(shared_dir) / f"{job.job_id}.notify"
        try:
            notify_path.parent.mkdir(parents=True, exist_ok=True)
            notify_path.write_text(fallback_msg + "\n", encoding="utf-8")
            _append_log(job.job_id, shared_dir,
                        f"Operator notification written to {notify_path}")
        except Exception as exc:
            _append_log(job.job_id, shared_dir,
                        f"Could not write notification file: {exc}")


# ── Interface contract extraction ────────────────────────────────────────────

def _extract_interface_contract(
    job: ForgeJob,
    final_impl: str,
    shared_dir: Path,
    api_key: str,
    extractor_model: str,
) -> dict:
    """
    Ask the LLM to extract the stable external interface surfaces from the final
    implementation and return them as a structured dict.

    Called after the build/critique cycle completes, before the approval gate.
    The result is stored in job.context_snapshot["interface_contract"] and written
    to the manifest by _apply_forge_output after operator approval.

    Returns an empty dict if the LLM call fails or the response cannot be parsed.
    """
    if not final_impl.strip():
        return {}

    _append_log(job.job_id, shared_dir, "Interface extraction: starting")

    # Keep the prompt focused — we only need interface surfaces, not a full review
    user_message = (
        "## Implementation to Analyse\n\n"
        + final_impl[:40_000]  # cap to stay within token budget
        + ("\n\n[truncated]" if len(final_impl) > 40_000 else "")
        + "\n\n## Task\n\nExtract the external interface surfaces from this implementation "
        "as described in your instructions. Return only the JSON object."
    )

    try:
        raw = _call_anthropic(
            system_prompt=BUILTIN_EXTRACTOR_PROMPT,
            user_message=user_message,
            model=extractor_model,
            api_key=api_key,
            max_tokens=2048,
        )
    except Exception as exc:
        _append_log(job.job_id, shared_dir, f"Interface extraction LLM call failed: {exc}")
        return {}

    # Parse the JSON response — find the outermost { } block
    start = raw.find("{")
    end   = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        _append_log(job.job_id, shared_dir, "Interface extraction: could not find JSON in response")
        return {}
    try:
        contract = json.loads(raw[start:end + 1])
        if not isinstance(contract, dict):
            return {}
        contract["populated_by_forge"] = True
        contract["extracted_at"] = now_iso()
        _append_log(
            job.job_id, shared_dir,
            f"Interface extraction complete: {len(contract.get('data_files', []))} data files, "
            f"{len(contract.get('cli', []))} CLI commands, "
            f"{len(contract.get('key_paths', {}))} key paths"
        )
        return contract
    except (json.JSONDecodeError, ValueError) as exc:
        _append_log(job.job_id, shared_dir, f"Interface extraction: JSON parse failed: {exc}")
        return {}


def _reconcile_build_spec(
    final_impl: str,
    original_spec: str,
    job: "ForgeJob",
    shared_dir: Path,
    api_key: str,
    model: str,
) -> str:
    """
    Ask the LLM to rewrite *original_spec* so it matches *final_impl* exactly.

    Called during Phase 5 (apply) after operator approval.  The reconciled spec
    is written back to ``manifest.build_spec`` so future improvement runs start
    from an accurate description of what is actually installed.

    Returns the reconciled spec text, or *original_spec* on any failure.
    """
    if not final_impl.strip():
        return original_spec

    _append_log(job.job_id, shared_dir, "Spec reconciliation: starting")

    user_message = (
        "## Original Build Specification\n\n"
        + (original_spec or "(none)")
        + "\n\n---\n\n"
        "## Final Implementation (what was actually built)\n\n"
        + final_impl[:40_000]
        + ("\n\n[truncated]" if len(final_impl) > 40_000 else "")
        + "\n\n---\n\n"
        "Rewrite the build specification to match the final implementation exactly. "
        "Return only the updated specification text."
    )

    try:
        reconciled = _call_anthropic(
            system_prompt=BUILTIN_RECONCILE_PROMPT,
            user_message=user_message,
            model=model,
            api_key=api_key,
            max_tokens=4096,
        )
        _append_log(job.job_id, shared_dir,
                    f"Spec reconciliation: complete ({len(reconciled)} chars)")
        return reconciled.strip()
    except Exception as exc:
        _append_log(job.job_id, shared_dir,
                    f"Spec reconciliation: failed (non-fatal): {exc}")
        return original_spec


# ── Apply (Phase 5) ───────────────────────────────────────────────────────────

def _run_static_analysis_phase(
    job: ForgeJob,
    manifest: ApplicationManifest | None,
    shared_dir: Path,
    *,
    api_key: str,
    critic_model: str,
    current_files: list[dict],
) -> dict:
    """Phase 2.5 — orphan + constraint + negative-path + env-portability.

    Spec: docs/spec-forge-side-effects-2026-06-02.md §13 (PR 6) + §14
    (PR 7). All pure-Python checks layered onto the forge critic cycle in
    advisory mode (findings are logged + surfaced for operator review but
    don't yet block approval or auto-refine — that's a follow-up).

    Returns a summary dict with counts + the raw findings; caller
    stamps it on ``job.context_snapshot["static_analysis_findings"]``.
    """
    from . import (
        orphan_check, constraint_critic, negative_path_tests,
        env_portability_lint,
    )

    summary: dict = {
        "orphans": [],
        "constraint_findings": [],
        "negative_path_tests": [],
        "env_portability_findings": [],
        "privacy_findings": [],
        "orphan_count": 0,
        "constraint_absent_count": 0,
        "negative_path_test_count": 0,
        "env_portability_count": 0,
        "privacy_undeclared_count": 0,
    }
    if manifest is None:
        return summary

    # Resolve the bot's workspace for orphan_check. Defensive when the
    # bot's home isn't accessible (test isolation, etc.) — orphan_check
    # short-circuits to [] on missing paths.
    workspace = Path(f"/Users/{manifest.bot_id}/.openclaw/workspace")
    if not workspace.exists():
        workspace = Path(f"/Users/{job.bot_id}/.openclaw/workspace")

    # ── §13.4 orphan check (pure Python; cheap) ───────────────────────────
    try:
        files = [
            (rec.get("path") or "").lstrip("/")
            for rec in (manifest.files or [])
            if isinstance(rec, dict)
        ]
        py_files = [f for f in files if f.endswith(".py")]
        if py_files and workspace.exists():
            orphans = orphan_check.find_orphans(workspace, files=py_files)
            summary["orphans"] = [o.to_dict() for o in orphans]
            summary["orphan_count"] = len(orphans)
    except Exception as exc:
        _append_log(job.job_id, shared_dir,
                    f"Phase 2.5 orphan_check failed (non-fatal): {exc}")

    # ── §13.2 constraint critic (LLM; uses current critic model) ──────────
    # Build the implementation_files dict from current_files. Best-effort
    # — empty dict on resolution failure means the critic gets no code
    # context and returns "unclear" for every item (acceptable degrade).
    try:
        impl_files: dict[str, str] = {}
        for rec in current_files or []:
            if not isinstance(rec, dict):
                continue
            path = (rec.get("path") or "").lstrip("/")
            content = rec.get("content") or ""
            if path and isinstance(content, str):
                impl_files[path] = content

        # Only run if there are any constraint items AND we have an api_key.
        # No-op when either is missing — the call_llm wrapper would just
        # raise.
        if api_key:
            items = constraint_critic.extract_constraint_items(
                _manifest_to_dict_for_critic(manifest)
            )
            if items:
                def _llm(system: str, user: str) -> str:
                    return _call_anthropic(
                        system_prompt=system, user_message=user,
                        model=critic_model, api_key=api_key,
                        max_tokens=4096,
                    )

                findings = constraint_critic.verify_constraints(
                    _manifest_to_dict_for_critic(manifest), impl_files, _llm,
                    items=items,
                )
                summary["constraint_findings"] = [f.to_dict() for f in findings]
                summary["constraint_absent_count"] = sum(
                    1 for f in findings if f.verdict == "absent"
                )
    except Exception as exc:
        _append_log(job.job_id, shared_dir,
                    f"Phase 2.5 constraint_critic failed (non-fatal): {exc}")

    # ── privacy block vs implementation (LLM; manifest-v7 Slice 2) ────────
    # "Does the privacy block match what the blueprint actually collects"
    # (docs/spec-manifest-v7-slicing-2026-06-10.md §4.1). Advisory like
    # the rest of Phase 2.5; ``undeclared_collection`` findings are the
    # signal — code collecting user data the consent notice omits.
    try:
        if api_key and getattr(manifest, "privacy", None):
            impl_files_p: dict[str, str] = {}
            for rec in current_files or []:
                if not isinstance(rec, dict):
                    continue
                path = (rec.get("path") or "").lstrip("/")
                content = rec.get("content") or ""
                if path and isinstance(content, str):
                    impl_files_p[path] = content

            def _llm_privacy(system: str, user: str) -> str:
                return _call_anthropic(
                    system_prompt=system, user_message=user,
                    model=critic_model, api_key=api_key,
                    max_tokens=4096,
                )

            privacy_findings = constraint_critic.verify_privacy_block(
                _manifest_to_dict_for_critic(manifest), impl_files_p,
                _llm_privacy,
            )
            summary["privacy_findings"] = [f.to_dict() for f in privacy_findings]
            summary["privacy_undeclared_count"] = sum(
                1 for f in privacy_findings if f.kind == "undeclared_collection"
            )
    except Exception as exc:
        _append_log(job.job_id, shared_dir,
                    f"Phase 2.5 privacy_block critic failed (non-fatal): {exc}")

    # ── §13.3 negative-path test extraction (pure regex; cheap) ───────────
    try:
        np_tests = negative_path_tests.extract_negative_path_tests(
            _manifest_to_dict_for_critic(manifest)
        )
        summary["negative_path_tests"] = [t.to_dict() for t in np_tests]
        summary["negative_path_test_count"] = len(np_tests)
    except Exception as exc:
        _append_log(job.job_id, shared_dir,
                    f"Phase 2.5 negative_path extraction failed (non-fatal): {exc}")

    # ── §14 env portability lint (pure regex; cheap) ──────────────────────
    # Catches Cluster-C: hardcoded /Users/Shared/evolve-venv/bin/python3,
    # systemsetup -gettimezone + UTC fallback, and the broader family of
    # sudo-required macOS commands. Pass the full manifest dict so the
    # lint can honor `requirements.system[]` and `requirements.privileged`
    # exemptions.
    try:
        if workspace.exists():
            file_paths = [
                (rec.get("path") or "").lstrip("/")
                for rec in (manifest.files or [])
                if isinstance(rec, dict)
            ]
            file_paths = [p for p in file_paths if p]
            if file_paths:
                manifest_dict = _manifest_full_dict_for_lint(manifest)
                portability_findings = env_portability_lint.lint_files(
                    workspace, file_paths, manifest=manifest_dict,
                )
                summary["env_portability_findings"] = [
                    f.to_dict() for f in portability_findings
                ]
                summary["env_portability_count"] = len(portability_findings)
    except Exception as exc:
        _append_log(job.job_id, shared_dir,
                    f"Phase 2.5 env_portability_lint failed (non-fatal): {exc}")

    return summary


def _manifest_to_dict_for_critic(manifest: ApplicationManifest) -> dict:
    """Adapt an ApplicationManifest to the dict shape the static analysis
    helpers consume. They only need a subset (constraints, identity,
    build_spec, files, privacy) so we pull just those rather than a full
    asdict() that would carry forge-internal fields the helpers don't
    expect.
    """
    return {
        "constraints": dict(manifest.constraints) if manifest.constraints else {},
        "identity": dict(manifest.identity) if manifest.identity else {},
        "build_spec": manifest.build_spec or "",
        "files": list(manifest.files or []),
        # v24: the privacy critic (constraint_critic.verify_privacy_block)
        # checks the declared block against what the code collects.
        "privacy": dict(manifest.privacy) if manifest.privacy else {},
    }


def _manifest_full_dict_for_lint(manifest: ApplicationManifest) -> dict:
    """Adapt to the shape env_portability_lint expects — includes
    ``bot_id`` (for workspace-path exemption) and ``requirements``
    (for system-path + privileged exemptions). Distinct from the critic
    adapter because the lint cares about different fields and we don't
    want one adapter to grow unwieldy.
    """
    return {
        "bot_id": manifest.bot_id or "",
        "requirements": (
            dict(manifest.requirements) if manifest.requirements else {}
        ),
        "files": list(manifest.files or []),
    }


# Parses an `every: "30m"` / `"2h"` / `"1d"` operator-facing shorthand
# into ``{"every_minutes": N}`` for the install helper. Defined at module
# scope so the deriver + admin UI can reuse it later.
_DURATION_RE = re.compile(r"^\s*(\d+)\s*(s|m|h|d)\s*$", re.IGNORECASE)


def _parse_every_duration(s: str) -> int | None:
    """Translate ``"30m"`` / ``"2h"`` / ``"1d"`` into a minute count.

    Returns ``None`` on any parse failure or for sub-minute durations
    (launchd's effective minimum granularity is 1 minute).
    """
    if not isinstance(s, str):
        return None
    m = _DURATION_RE.match(s)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    minutes_per_unit = {"s": 0, "m": 1, "h": 60, "d": 60 * 24}
    if unit == "s":
        # Round 60s up to 1 minute, drop anything smaller.
        minutes = max(0, n // 60)
    else:
        minutes = n * minutes_per_unit[unit]
    return minutes if minutes >= 1 else None


def _normalise_schedule(install_cfg: dict) -> tuple[dict | None, str | None]:
    """Translate the operator-facing schedule shape into the dict the
    ``install_python_signal_action`` helper accepts.

    Accepts (in this priority order):
      install.every          ("30m" / "2h" / "1d") — operator shorthand
      install.every_minutes  (int)                  — unambiguous form
      install.schedule       (dict)                  — passthrough
                                                       (every_minutes OR cron)

    Returns ``(schedule_dict, error)``. On error, the dict is None and
    the error string explains what to fix.
    """
    every = install_cfg.get("every")
    if every is not None:
        minutes = _parse_every_duration(every)
        if minutes is None:
            return None, (
                f"install.every must be a duration like '30m' / '2h' / '1d' "
                f"and >= 1 minute; got {every!r}"
            )
        return {"every_minutes": minutes}, None

    if "every_minutes" in install_cfg:
        try:
            minutes = int(install_cfg["every_minutes"])
        except (TypeError, ValueError):
            return None, (
                "install.every_minutes must be an integer; got "
                f"{install_cfg['every_minutes']!r}"
            )
        if minutes < 1:
            return None, "install.every_minutes must be >= 1"
        return {"every_minutes": minutes}, None

    schedule = install_cfg.get("schedule")
    if isinstance(schedule, dict) and schedule:
        # Operator passed the raw helper-shape dict. Validate keys.
        if "every_minutes" in schedule or "cron" in schedule:
            return schedule, None
        return None, (
            "install.schedule must declare either 'every_minutes' or 'cron'"
        )

    return None, (
        "install must declare a schedule: one of install.every "
        "('30m'/'2h'/'1d'), install.every_minutes (int), or "
        "install.schedule (dict)"
    )


def _read_gallery_package_manifest(pkg_id: str) -> dict | None:
    """Load the in-repo gallery package manifest for ``pkg_id``.

    Returns the raw dict or None when no such package exists on
    disk. Used by F-P.13.c to read the package's provenance + the
    contributor's public key for signature verification.
    """
    from .gallery import _BUILTIN_GALLERY_DIR
    try:
        for app_dir in _BUILTIN_GALLERY_DIR.iterdir():
            if not app_dir.is_dir():
                continue
            cand = app_dir / f"{pkg_id}.json"
            if cand.is_file():
                return json.loads(cand.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _verify_community_signature_or_raise(
    job: ForgeJob,
    pack_metadata: "FilesPackMetadata",
    shared_dir: Path,
) -> None:
    """F-P.13.c — signature gate for community-sourced files-packs.

    Policy v1:
      - evolve / operator-local provenance → no-op (trust by storage)
      - community + signature absent → log warning, allow install
      - community + signature present + valid → log success, allow
      - community + signature present + invalid → raise
        ``FilesPackSignatureRefused`` (NOT caught by the broad except
        in _maybe_install_via_files_pack; propagates out so the
        LLM-forge fall-through doesn't silently bypass the check)

    This sits AFTER integrity verification — content drift is
    caught by SHA mismatch, and only signature-specific outcomes
    reach the verifier.
    """
    from .files_pack_signing import (
        FilesPackSignatureRefused,
        load_public_key_pem,
        verify_files_pack_signature,
    )

    package_manifest = _read_gallery_package_manifest(job.pkg_id) or {}
    provenance = (package_manifest.get("provenance") or "").strip()
    if not provenance:
        # No explicit provenance on the package manifest. By default
        # it's "evolve" for builtin (the only path that lands here
        # via _BUILTIN_GALLERY_DIR). Skip the check.
        return
    if provenance != "community":
        # evolve / operator-local — trust by storage location.
        return

    signature = pack_metadata.signature or {}
    contributor = package_manifest.get("contributor") or {}
    public_key_pem = (contributor.get("public_key") or "").strip()

    if not signature:
        _append_log(
            job.job_id, shared_dir,
            f"Files-pack signature: community package {job.pkg_id} has "
            f"no signature — allowing install but operator should review "
            f"the contributor before trusting future updates.",
        )
        return

    if not public_key_pem:
        # Signature present but no public key to verify against. This
        # is a misconfigured package — we can't establish trust.
        raise FilesPackSignatureRefused(
            "missing_public_key",
            detail=(
                f"package {job.pkg_id} has provenance=community + a "
                f"signature but no contributor.public_key to verify "
                f"against"
            ),
        )

    try:
        pubkey = load_public_key_pem(public_key_pem.encode("utf-8"))
    except Exception as exc:
        raise FilesPackSignatureRefused(
            "malformed_public_key",
            detail=f"could not parse contributor.public_key: {exc}",
        )

    ok, reason = verify_files_pack_signature(
        pack_metadata, signature, pubkey,
    )
    if ok:
        _append_log(
            job.job_id, shared_dir,
            f"Files-pack signature: community package {job.pkg_id} "
            f"verified against contributor.public_key "
            f"(signer_key_id={signature.get('signer_key_id', '')[:24]}…)",
        )
        return
    raise FilesPackSignatureRefused(
        reason,
        detail=(
            f"package {job.pkg_id} signature did not verify against "
            f"contributor.public_key"
        ),
    )


def _install_partial_files_pack(
    job: ForgeJob,
    partial_plan: dict,
    shared_dir: Path,
) -> list[dict]:
    """Install the bundled subset of a partial files-pack (F-P.11.b).

    Called by the Step 2 dispatcher AFTER ``bot_forge.dispatch_build``
    has written the forge gap. Reads the plan stashed in
    ``job.context_snapshot['files_pack_partial']`` and replays the
    F-P.2 install path against just the bundled paths.

    Returns a list of ``{path, sha256}`` dicts for the files actually
    written. Empty list when something goes wrong (logged, not
    raised; the LLM-forge result has already been accepted upstream).
    """
    from .files_pack import (
        FilesPackError,
        install_files_pack_to_workspace,
        load_files_pack_metadata,
    )
    try:
        files_pack_dir = Path(partial_plan["files_pack_dir"])
        workspace = Path(partial_plan["workspace"])
        context = dict(partial_plan["context"])
        bundled_paths = set(partial_plan.get("bundled_paths") or [])
    except (KeyError, TypeError) as exc:
        _append_log(
            job.job_id, shared_dir,
            f"Partial files-pack install: bad plan shape ({exc}) — "
            f"bundled files NOT installed; LLM-forge output kept verbatim",
        )
        return []

    if not bundled_paths:
        return []

    try:
        pack_metadata = load_files_pack_metadata(files_pack_dir)
        if pack_metadata is None:
            _append_log(
                job.job_id, shared_dir,
                f"Partial files-pack install: metadata missing at "
                f"{files_pack_dir} — bundled files NOT installed",
            )
            return []
        install_result = install_files_pack_to_workspace(
            pack_metadata, files_pack_dir, workspace, context,
            allowed_paths=bundled_paths,
        )
        if install_result.errors:
            details = "; ".join(install_result.errors[:3])
            _append_log(
                job.job_id, shared_dir,
                f"Partial files-pack install: {len(install_result.errors)} "
                f"write error(s) — bundled files only partially landed: "
                f"{details}",
            )
        _append_log(
            job.job_id, shared_dir,
            f"Partial files-pack install: wrote "
            f"{len(install_result.files_written)} bundled file(s) on top "
            f"of LLM-forge output",
        )
        return install_result.files_written
    except FilesPackError as exc:
        _append_log(
            job.job_id, shared_dir,
            f"Partial files-pack install failed: {exc} — bundled files "
            f"NOT installed",
        )
        return []
    except Exception as exc:  # noqa: BLE001
        _append_log(
            job.job_id, shared_dir,
            f"Partial files-pack install crashed: "
            f"{type(exc).__name__}: {exc} — bundled files NOT installed",
        )
        return []


def _maybe_install_via_files_pack(
    job: ForgeJob,
    manifest: ApplicationManifest | None,
    shared_dir: Path,
    *,
    install_callback: "Callable | None" = None,
) -> Any:
    """If the manifest carries a files_pack metadata + the gallery has
    canonical files for the package, install via copy + substitution
    and return a BuildResult-shaped object. Returns ``None`` to fall
    through to the existing LLM-forge path.

    F-P.11.b: when the manifest declares a partial files-pack (some
    files marked ``bundled`` AND some ``forge``), this function does
    NOT install the bundled subset here — the caller (Step 2 dispatcher)
    needs the LLM to build the forge gap first, then runs ``install_callback``
    (or replays this function with the right shape) to write the
    bundled files. The partial case stashes the plan in
    ``job.context_snapshot['files_pack_partial']`` so Step 2 can
    finish the work after LLM-forge returns.

    Spec: docs/spec-files-pack-hybrid-2026-06-03.md §7. The cost lever:
    when this returns non-None, Phases 1-3 are replaced by a
    pure-Python file IO pass that costs ~$0 instead of $30+.

    Best-effort: any failure (missing gallery files, integrity
    mismatch, substitution error) returns None and the caller falls
    through to LLM-forge. The error is logged so an operator can
    investigate without blocking the install.
    """
    if manifest is None:
        return None
    files_pack_meta = manifest.files_pack or {}
    if not files_pack_meta.get("format_version"):
        return None
    if not job.pkg_id:
        return None

    # Lazy imports — the LLM-forge path doesn't need the files_pack
    # module loaded, and gallery.py already exists upstream.
    from .gallery import find_files_pack_dir
    from .files_pack import (
        FilesPackError,
        compute_files_pack_sha256,
        install_files_pack_to_workspace,
        load_files_pack_metadata,
        resolve_install_context,
        verify_files_pack_integrity,
    )

    files_pack_dir = find_files_pack_dir(job.pkg_id)
    if files_pack_dir is None:
        _append_log(
            job.job_id, shared_dir,
            f"Files-pack: manifest declares files_pack but gallery dir "
            f"for {job.pkg_id} missing — falling through to LLM-forge",
        )
        return None

    try:
        pack_metadata = load_files_pack_metadata(files_pack_dir)
        if pack_metadata is None:
            _append_log(
                job.job_id, shared_dir,
                f"Files-pack: {files_pack_dir} has no manifest.json — "
                f"falling through to LLM-forge",
            )
            return None

        # Stale check — operator may have updated the gallery files
        # without bumping the package manifest's sha256. We warn but
        # proceed; the per-file integrity check below catches actual
        # content drift.
        declared_sha = (files_pack_meta.get("sha256") or "").strip().lower()
        if declared_sha:
            actual_sha = compute_files_pack_sha256(files_pack_dir).lower()
            if actual_sha != declared_sha:
                _append_log(
                    job.job_id, shared_dir,
                    f"Files-pack: top-level sha256 mismatch "
                    f"(declared={declared_sha[:12]}…, "
                    f"actual={actual_sha[:12]}…); proceeding with on-disk "
                    f"version",
                )

        integrity_findings = verify_files_pack_integrity(
            files_pack_dir, pack_metadata,
        )
        if integrity_findings:
            details = ", ".join(
                f"{f.kind}:{f.path}" for f in integrity_findings[:5]
            )
            _append_log(
                job.job_id, shared_dir,
                f"Files-pack: {len(integrity_findings)} integrity "
                f"finding(s): {details} — falling through to LLM-forge",
            )
            return None

        # F-P.13.c — signature verification for community packages.
        # Distinct from integrity (which is content-equivalence). The
        # signature confirms the bytes came from the declared
        # contributor and haven't been tampered with since.
        #
        # Policy v1:
        #   - evolve / operator-local provenance → skip verification
        #     (trust by storage location)
        #   - community + signature present + valid → continue
        #   - community + signature present + invalid → REFUSE install
        #     (raises FilesPackSignatureRefused — propagates out so the
        #     LLM-forge path doesn't silently bypass the check)
        #   - community + signature absent → log a warning, continue
        #     (operators can manually require signatures via a future
        #     flag; v1 is soft to keep unsigned community packages
        #     installable)
        _verify_community_signature_or_raise(
            job, pack_metadata, shared_dir,
        )

        # Resolve install context. Failure here means the bot isn't
        # properly resolvable — fall through to LLM-forge which will
        # surface the same error via the normal path.
        from ..config import bot_home, get_bot_user, load_network
        network = load_network()
        bot_user = get_bot_user(job.bot_id, network)
        workspace = bot_home(job.bot_id, network) / ".openclaw" / "workspace"
        context = resolve_install_context(
            bot_id=job.bot_id,
            bot_user=bot_user,
            workspace=str(workspace),
            pkg_id=job.pkg_id,
            app_id=job.app_id or manifest.id or "",
            installed_at=now_iso(),
            shared_dir=str(shared_dir),
        )

        # Smart-forge dispatcher (docs/note-smart-forge-and-file-
        # provenance-2026-06-04.md): partition manifest.files[] by
        # provenance. Install only the "bundled" subset via files-pack;
        # if there's a "forge" subset, fall through to LLM-forge to
        # produce those — Phase 4.5 runs regardless of split.
        pack_paths = {f.path for f in pack_metadata.files}
        partition = manifest.files_partition(files_pack_paths=pack_paths)
        forge_paths = partition.get("forge") or []
        bundled_paths = set(partition.get("bundled") or [])
        # When the manifest doesn't declare files[] at all (legacy /
        # gallery-stub case), partition returns both lists empty.
        # Fall back to installing every file in the pack — the pack
        # metadata is the safety net.
        manifest_has_files = bool(manifest.files or [])
        if manifest_has_files and forge_paths:
            # Mixed-mode (F-P.11.b): some files come from the pack,
            # others need LLM generation. Stash the install plan in
            # context_snapshot so Step 2 can finish bundled-file
            # installation AFTER LLM-forge writes the forge gap. The
            # LLM is told to skip the bundled paths via
            # BuildRequest.paths_already_covered.
            job.context_snapshot["files_pack_partial"] = {
                "files_pack_dir": str(files_pack_dir),
                "bundled_paths": sorted(bundled_paths),
                "forge_paths": sorted(forge_paths),
                "context": dict(context),
                "workspace": str(workspace),
            }
            _append_log(
                job.job_id, shared_dir,
                f"Files-pack: partial coverage "
                f"({len(bundled_paths)} bundled, {len(forge_paths)} forge) — "
                f"stashed install plan; LLM-forge will build forge paths "
                f"with paths_already_covered set; bundled subset installs "
                f"after LLM completes",
            )
            return None

        # Complete coverage. Two cases:
        # 1. Manifest declared files[] AND every entry is bundled
        #    -> install only the bundled subset (allowed_paths filter).
        # 2. Manifest didn't declare files[] (legacy stub)
        #    -> install everything in the pack (allowed_paths=None).
        allowed_paths_filter = bundled_paths if manifest_has_files else None
        install_result = install_files_pack_to_workspace(
            pack_metadata, files_pack_dir, workspace, context,
            allowed_paths=allowed_paths_filter,
        )
        if install_result.errors:
            details = "; ".join(install_result.errors[:3])
            _append_log(
                job.job_id, shared_dir,
                f"Files-pack: {len(install_result.errors)} write "
                f"error(s) — falling through to LLM-forge: {details}",
            )
            return None

        _append_log(
            job.job_id, shared_dir,
            f"Files-pack install: wrote {len(install_result.files_written)} "
            f"file(s) ({install_result.bytes_total} bytes total) — "
            f"skipped LLM build/critique/refine",
        )

        # Shape the result like bot_forge.BuildResult so the downstream
        # verification + manifest-records + critique-skip logic all
        # work unchanged. Defining inline to avoid a circular import
        # from forge_engine -> bot_forge -> ... when forge_engine
        # already imports bot_forge for the LLM path.
        from .bot_forge import BuildResult
        return BuildResult(
            status="complete",
            files_written=install_result.files_written,
            test_run=None,
            test_exit_code=None,
            test_output="",
            notes=(
                f"files-pack install: {len(install_result.files_written)} "
                f"files, {install_result.bytes_total} bytes, "
                f"~$0 LLM cost"
            ),
            raw={
                "install_path": "files_pack",
                "format_version": pack_metadata.format_version,
                "snapshot_source": pack_metadata.snapshot_source,
            },
            agent_exit_code=0,
        )
    except FilesPackError as exc:
        _append_log(
            job.job_id, shared_dir,
            f"Files-pack install failed: {exc} — falling through to "
            f"LLM-forge",
        )
        return None
    except Exception as exc:  # noqa: BLE001 — log + fallthrough
        # F-P.13.c — FilesPackSignatureRefused must propagate out.
        # Falling through to LLM-forge on signature refusal would
        # silently bypass the security check — the whole install
        # has to abort.
        from .files_pack_signing import FilesPackSignatureRefused
        if isinstance(exc, FilesPackSignatureRefused):
            _append_log(
                job.job_id, shared_dir,
                f"Files-pack signature REFUSED: {exc.reason} "
                f"({exc.detail}) — aborting install (will NOT fall "
                f"through to LLM-forge)",
            )
            raise
        _append_log(
            job.job_id, shared_dir,
            f"Files-pack install crashed: {type(exc).__name__}: {exc} — "
            f"falling through to LLM-forge",
        )
        return None


def _materialize_scheduled_actions(
    job: ForgeJob,
    manifest: ApplicationManifest,
) -> list[dict]:
    """Phase 4.5 — install the side-effects each scheduled_action declares.

    Walks ``manifest.scheduled_actions[]`` and dispatches to
    ``install_helpers`` by mechanism:

      launchd_python_signal                → install_python_signal_action
                                             (v18 default for periodic
                                              checks: launchd-scheduled
                                              Python wrapper that only
                                              escalates to the bot LLM via
                                              the Signal store when a
                                              signal pattern matches —
                                              spec-launchd-python-signal)
      oc_heartbeat_instruction / oc_session_instruction
                                           → install_heartbeat_instruction
                                             (writes managed section to
                                              HEARTBEAT.md / AGENTS.md;
                                              v17 mechanism, now narrowed
                                              to checks that need the LLM
                                              every heartbeat)
      launchd                              → install_launch_agent
      crontab                              → install_crontab_entry (returns
                                              error in PR 4 — see helper
                                              docstring for the sudoers gap)
      external / unknown                   → skipped (nothing to install)
      oc_heartbeat_hook / oc_session_hook → failed (deprecated in v17;
                                              the migration helper should
                                              have rewritten these. A
                                              manifest that still carries
                                              them needs migrate_manifest()
                                              before forge can act.)

    Spec: docs/spec-heartbeat-instruction-2026-06-03.md §4 (v17),
          docs/spec-launchd-python-signal-2026-06-03.md (v18).

    On success, mutates the action entry in-place to stamp
    ``installed_at``, ``installed_by``, and ``installed_artifact``. On
    failure, leaves the stamps absent and records the error in the
    returned summary.

    Returns a list of per-action status dicts:
        {action_id, mechanism, status: "ok"|"failed"|"skipped",
         artifact?: str, error?: str}

    Best-effort: a failure on one action does not halt the rest. The
    caller (``_apply_forge_output``) stamps the summary onto
    ``job.context_snapshot["scheduled_actions_installed"]`` so the
    operator review surface can render it.
    """
    from .install_helpers import (
        install_heartbeat_instruction,
        install_launch_agent,
        install_launchd_command_action,
        install_python_signal_action,
        install_crontab_entry,
    )

    summary: list[dict] = []
    actions = manifest.scheduled_actions or []
    if not isinstance(actions, list):
        return summary

    install_stamp = f"forge:{job.job_id}"
    installed_iso = now_iso()

    for action in actions:
        if not isinstance(action, dict):
            continue
        action_id = action.get("id") or "?"
        mechanism = (action.get("mechanism") or "").strip()
        install_cfg = action.get("install") or {}

        # Skip mechanisms forge can't materialize from this PR.
        if mechanism in ("", "unknown", "external"):
            summary.append({
                "action_id": action_id,
                "mechanism": mechanism or "unknown",
                "status": "skipped",
                "reason": "mechanism is external or pending scanner attribution",
            })
            continue

        # If the action was already installed by an earlier forge run AND
        # the install config hasn't changed, skip it idempotently. We can
        # only tell "hasn't changed" by checking ``installed_artifact`` —
        # a no-op install still re-stamps it, so we treat the artifact's
        # presence as "previously installed by forge."
        if (action.get("installed_artifact")
            and (action.get("installed_by") or "").startswith("forge:")):
            summary.append({
                "action_id": action_id,
                "mechanism": mechanism,
                "status": "skipped",
                "reason": "already installed by a prior forge run",
                "artifact": action.get("installed_artifact"),
            })
            continue

        try:
            if mechanism in ("oc_heartbeat_instruction", "oc_session_instruction"):
                # v17: install a managed section in HEARTBEAT.md / AGENTS.md.
                # The bot's session-driven LLM reads the file on the next
                # heartbeat (or session start) and executes the instruction.
                file = (install_cfg.get("file") or "").strip()
                section_anchor = (install_cfg.get("section_anchor") or "").strip()
                body = (install_cfg.get("body") or "").strip()
                missing = [
                    name for name, value in [
                        ("file", file),
                        ("section_anchor", section_anchor),
                        ("body", body),
                    ] if not value
                ]
                if missing:
                    summary.append({
                        "action_id": action_id,
                        "mechanism": mechanism,
                        "status": "failed",
                        "error": (
                            f"install requires {', '.join(missing)} for the "
                            f"{mechanism} mechanism"
                        ),
                    })
                    continue
                result = install_heartbeat_instruction(
                    manifest.bot_id, file, section_anchor, body,
                    pkg_id=manifest.pkg_id or "",
                    job_id=job.job_id or "",
                )
            elif mechanism in ("oc_heartbeat_hook", "oc_session_hook"):
                # Deprecated in v17. The migration helper
                # _migrate_scheduled_action_entry_v17 rewrites these on
                # manifest load, so this branch only fires when the
                # manifest hasn't been migrated yet (operator hand-edited
                # the file post-load, etc.). Surface a clear remediation.
                summary.append({
                    "action_id": action_id,
                    "mechanism": mechanism,
                    "status": "failed",
                    "error": (
                        f"mechanism {mechanism!r} is deprecated in v17 — "
                        f"run migrate_manifest() to rewrite this entry, "
                        f"then re-forge. "
                        f"See docs/spec-heartbeat-instruction-2026-06-03.md."
                    ),
                })
                continue
            elif mechanism == "launchd":
                # Two shapes are supported, in this order of precedence:
                #
                #   (a) Structured command shape (canonical, post-2026-06-04
                #       migration):
                #         install: {plist_label, command, schedule, cwd?, env?}
                #
                #   (b) Raw plist_xml shape (legacy; predates the validator):
                #         install: {plist_label, plist_xml}
                #
                # Shape (a) is preferred for every new manifest — it survives
                # ${bot_id} / ${workspace} substitution, is the gate that
                # ``scheduled_actions_validator`` checks, and materializes
                # through the scheduler seam (launchd plist on macOS, systemd
                # service+timer units on a Linux pod — the mechanism name is
                # historical). Shape (b) is raw launchd XML: macOS-only, kept
                # so older manifests already in the wild keep working until
                # they're re-forged.
                label = (install_cfg.get("plist_label") or "").strip()
                command = (install_cfg.get("command") or "").strip()
                schedule = install_cfg.get("schedule") or {}
                plist_xml = install_cfg.get("plist_xml", "")

                if not label:
                    summary.append({
                        "action_id": action_id,
                        "mechanism": mechanism,
                        "status": "failed",
                        "error": "install.plist_label is required for the launchd mechanism",
                    })
                    continue

                if command:
                    # Shape (a): build the plist via the structured installer.
                    if not isinstance(schedule, dict) or not schedule:
                        summary.append({
                            "action_id": action_id,
                            "mechanism": mechanism,
                            "status": "failed",
                            "error": (
                                "install.schedule must be a non-empty dict when "
                                "install.command is set (every_minutes=N OR cron={...})"
                            ),
                        })
                        continue
                    result = install_launchd_command_action(
                        manifest.bot_id,
                        action_id,
                        label,
                        command,
                        schedule,
                        cwd=(install_cfg.get("cwd") or "").strip(),
                        env=install_cfg.get("env") or None,
                    )
                elif plist_xml:
                    # Shape (b): legacy raw plist.
                    result = install_launch_agent(manifest.bot_id, label, plist_xml)
                else:
                    summary.append({
                        "action_id": action_id,
                        "mechanism": mechanism,
                        "status": "failed",
                        "error": (
                            "install must declare either install.command + install.schedule "
                            "(canonical) or install.plist_xml (legacy) for the launchd mechanism"
                        ),
                    })
                    continue
            elif mechanism == "launchd_python_signal":
                # v18 (2026-06-03): Python-by-default scheduled action.
                # Wrapper runs on the launchd schedule, parses stdout for
                # signal patterns, writes a Signal only when there's
                # something to surface. Most invocations are silent —
                # zero LLM cost.
                # Spec: docs/spec-launchd-python-signal-2026-06-03.md.
                label = (install_cfg.get("label") or "").strip()
                command = (install_cfg.get("command") or "").strip()
                patterns = install_cfg.get("signal_patterns") or []
                if isinstance(patterns, str):
                    # Tolerate operator-supplied single string by wrapping.
                    patterns = [patterns]
                missing = [
                    name for name, value in [
                        ("label", label),
                        ("command", command),
                        ("signal_patterns", patterns),
                    ] if not value
                ]
                if missing:
                    summary.append({
                        "action_id": action_id,
                        "mechanism": mechanism,
                        "status": "failed",
                        "error": (
                            f"install requires {', '.join(missing)} for the "
                            f"{mechanism} mechanism"
                        ),
                    })
                    continue
                schedule, sched_err = _normalise_schedule(install_cfg)
                if schedule is None:
                    summary.append({
                        "action_id": action_id,
                        "mechanism": mechanism,
                        "status": "failed",
                        "error": sched_err or "missing schedule",
                    })
                    continue
                result = install_python_signal_action(
                    manifest.bot_id,
                    action_id,
                    label,
                    command,
                    schedule,
                    list(patterns),
                    cwd=(install_cfg.get("cwd") or "").strip(),
                    signal_type=(install_cfg.get("signal_type") or "task_pending"),
                    signal_severity=(install_cfg.get("signal_severity") or "info"),
                    app_id=manifest.id or "",
                    pkg_id=manifest.pkg_id or "",
                    job_id=job.job_id or "",
                )
            elif mechanism == "crontab":
                schedule = (install_cfg.get("schedule") or "").strip()
                command = (install_cfg.get("command") or "").strip()
                label = (install_cfg.get("label") or "").strip()
                result = install_crontab_entry(
                    manifest.bot_id, schedule, command, label,
                )
            else:
                summary.append({
                    "action_id": action_id,
                    "mechanism": mechanism,
                    "status": "failed",
                    "error": f"unrecognized mechanism: {mechanism!r}",
                })
                continue
        except Exception as exc:
            summary.append({
                "action_id": action_id,
                "mechanism": mechanism,
                "status": "failed",
                "error": f"install helper raised: {type(exc).__name__}: {exc}",
            })
            continue

        if result.get("ok"):
            artifact = result.get("artifact") or ""
            # Stamp provenance on the action so the verifier can find the
            # install on the next audit run.
            action["installed_at"] = installed_iso
            action["installed_by"] = install_stamp
            if artifact:
                action["installed_artifact"] = artifact
            summary.append({
                "action_id": action_id,
                "mechanism": mechanism,
                "status": "ok",
                "artifact": artifact,
                "already_present": bool(result.get("already_present", False)),
            })
        else:
            summary.append({
                "action_id": action_id,
                "mechanism": mechanism,
                "status": "failed",
                "error": result.get("error") or "install helper returned no diagnostic",
            })

    return summary


def _record_scheduled_action_outcomes(
    job: ForgeJob,
    manifest: ApplicationManifest,
    install_summary: list[dict],
    shared_dir: Path,
) -> None:
    """Failure-visibility layer for Phase 4.5 (audit slate S2, 2026-07-02).

    Installs auto-ship and Phase 4.5 is per-action best-effort, so without
    the three stamps below a partially-materialized app is indistinguishable
    from a healthy one (the silent-dead-app class):

      1. ``install_error`` / ``install_failed_at`` on the manifest action
         entry — enumerable by the app card, uninstall teardown (S4), and
         the live-test harness (what installed vs declared); cleared when a
         later attempt installs (or idempotently skips) the action;
      2. ``job.completed_with_errors`` — the Forge Jobs list renders the
         outcome; job status stays "complete" (the non-scheduled parts of
         the app are real, and the sequential starter-pack installer keys
         off terminal status);
      3. a per-action Signal so the operator hears about it without waiting
         for delivery_monitor's first missed window (which never comes for
         non-user-facing actions).

    Caller must invoke this BEFORE saving the manifest so the entry stamps
    persist.
    """
    n_failed = sum(1 for e in install_summary if e.get("status") == "failed")
    actions_by_id = {
        a.get("id"): a
        for a in (manifest.scheduled_actions or [])
        if isinstance(a, dict) and a.get("id")
    }
    for entry in install_summary:
        action = actions_by_id.get(entry.get("action_id"))
        if action is None:
            continue
        if entry.get("status") == "failed":
            action["install_error"] = (
                entry.get("error") or "install failed (no diagnostic)"
            )
            action["install_failed_at"] = now_iso()
        else:
            action.pop("install_error", None)
            action.pop("install_failed_at", None)
    # Unconditional assignment (not a one-way latch): a re-run of the SAME
    # job object after a prior partial failure must be able to clear the
    # flag when the retry succeeds, or a clean run would still badge
    # "complete (errors)". Production retries clone into a fresh job (flag
    # defaults False), but apply-actions and any future same-job path get
    # correct behavior for free.
    job.completed_with_errors = n_failed > 0
    if n_failed:
        _append_log(
            job.job_id, shared_dir,
            f"Phase 4.5: {n_failed} of {len(install_summary)} scheduled "
            f"action(s) FAILED to materialize — job will complete with "
            f"errors (see scheduled_actions_installed for detail)",
        )
    _signal_scheduled_action_outcomes(
        job, manifest, install_summary, shared_dir,
    )


def _scheduled_action_failure_signature(bot_id: str, app_id: str, action_id: str) -> str:
    """Signature for a per-action materialize-failure Signal.

    Keyed on (bot, app, action) — NOT job_id — so a re-forge of the same
    app dedups into the existing Signal (observation_count bump) instead
    of minting a sibling per attempt, and a later successful install of
    the same action can find-and-resolve it.
    """
    return f"forge/scheduled_action_failed/{bot_id}/{app_id}/{action_id}"


def _signal_scheduled_action_outcomes(
    job: ForgeJob,
    manifest: ApplicationManifest,
    install_summary: list[dict],
    shared_dir: Path,
) -> None:
    """Make Phase 4.5 outcomes observable (audit slate S2, 2026-07-02).

    Before this, a partially-materialized app was indistinguishable from a
    healthy one: install jobs auto-approve, Phase 4.5 is per-action
    best-effort, and the manifest saves ``status="approved"`` regardless —
    the silent-dead-app class (#3392's Linux case is one instance; a launchd
    bootstrap failure on macOS is another). This helper is the visibility
    layer:

      - each ``failed`` entry emits a Signal (producer ``forge_engine``,
        type ``forge_scheduled_action_failed``) so the Alerts page and the
        chat notifier surface it without waiting for delivery_monitor's
        first missed window — which never comes for non-user-facing actions;
      - each non-failed entry resolves any prior failure Signal for the
        same (bot, app, action) — a successful re-forge quiets the alert
        without a sweep (a producer-wide sweep_resolve would wrongly
        archive OTHER apps' still-live failures, since one run only
        re-checks one app's actions).

    Best-effort by contract: any exception logs and returns — Signal
    plumbing must never fail an apply that already happened.
    """
    try:
        from signals import store as _signals_store  # type: ignore[import]
    except Exception as exc:
        _append_log(job.job_id, shared_dir,
                    f"Phase 4.5: signals store unavailable (non-fatal): {exc}")
        return

    declared = len([a for a in (manifest.scheduled_actions or [])
                    if isinstance(a, dict)])
    for entry in install_summary:
        action_id = str(entry.get("action_id") or "?")
        signature = _scheduled_action_failure_signature(
            job.bot_id, job.app_id, action_id)
        try:
            _signal_one_scheduled_action_outcome(
                _signals_store, job, entry, signature, declared,
                install_summary, shared_dir,
            )
        except Exception as exc:
            _append_log(
                job.job_id, shared_dir,
                f"Phase 4.5: outcome Signal for action {action_id!r} "
                f"failed (non-fatal): {exc}",
            )


def _signal_one_scheduled_action_outcome(
    _signals_store,
    job: ForgeJob,
    entry: dict,
    signature: str,
    declared: int,
    install_summary: list[dict],
    shared_dir: Path,
) -> None:
    """Emit-or-resolve for a single Phase 4.5 summary entry (see caller)."""
    action_id = str(entry.get("action_id") or "?")
    if entry.get("status") == "failed":
        _signals_store.observe(
            shared_dir,
            signature=signature,
            producer="forge_engine",
            type="forge_scheduled_action_failed",
            flavor="maintenance",
            severity="warn",
            scope="bot",
            bot_id=job.bot_id,
            title=(
                f"{job.bot_id}/{job.app_id}: scheduled action "
                f"{action_id!r} failed to install"
            ),
            body=(
                f"Forge job {job.job_id} completed, but the "
                f"{entry.get('mechanism') or 'unknown'} install for "
                f"scheduled action {action_id!r} failed: "
                f"{entry.get('error') or '(no diagnostic)'}\n\n"
                f"The app's files and instructions shipped; this "
                f"schedule did not. Until it installs, the action "
                f"never fires — re-install the app to retry its "
                f"setup, or fix the underlying cause first if it "
                f"keeps failing."
            ),
            details={
                "bot_id": job.bot_id,
                "app_id": job.app_id,
                "job_id": job.job_id,
                "action_id": action_id,
                "mechanism": entry.get("mechanism"),
                "error": entry.get("error"),
                "declared_actions": declared,
                "installed_actions": sum(
                    1 for e in install_summary if e.get("status") == "ok"),
            },
        )
    else:
        # ok, or skipped (already-installed / external) — a prior
        # failure for this exact action is no longer live. Targeted
        # resolve; no-op when no active Signal carries the signature.
        sig = _signals_store.find_active_by_signature(shared_dir, signature)
        if sig is not None:
            _signals_store.apply_transition(
                sig, "resolved", shared_dir,
                actor="forge_engine",
                reason=f"scheduled action installed by forge job {job.job_id}",
            )


# ── agent-freelance-bypass: default forged script apps to plugin_intercept ────
#
# Spec: docs/spec-agent-freelance-bypass-phase2-2026-06-06.md.
#
# When a forged app's bot_guidance tells the LLM to run a bot-local script and
# that script fails at runtime, OpenClaw leaks a raw "(agent) failed" marker
# into chat AND the agent freelances a confabulated success
# (docs/spec-agent-freelance-bypass-2026-06-05.md). The structural fix is
# invocation_mode='plugin_intercept': the plugin runs the script in
# before_prompt_build (no LLM tool-call, no leak) and posts a controlled
# fallback_text on non-zero exit. But forge has historically shipped every new
# app in the leaky default 'agent_invokes'. This normalizer flips that default
# for at-risk-shaped apps and synthesizes the invocation contract Layer C +
# the install-time validator require.

# Reply posted verbatim by Layer C when a normalized script exits non-zero and
# emits nothing on stdout (the on_failure='post_fallback' path). Generic by
# design — the app author can override per-trigger via an authored
# invocation.fallback_text, which this normalizer preserves.
_DEFAULT_FREELANCE_FALLBACK = (
    "Sorry — I couldn't complete that just now. Please try again in a few minutes."
)

# Mines the script path out of at-risk bot_guidance prose. Matches the
# canonical "run … python3 scripts/<name>.py" shape the validator's
# _AT_RISK_PROSE_MARKERS keys on, capturing the relative script path.
_PROSE_SCRIPT_RE = re.compile(
    r"python3\s+['\"`]?(scripts/[\w./-]+\.py)", re.IGNORECASE
)

# stdout protocols the plugin can actually parse (mirror of the validator's
# _KNOWN_STDOUT_PROTOCOLS / triggerProtocols.ts KNOWN_PROTOCOLS). 'raw_text' is
# the generic one forge stamps; the atlas_* protocols are preserved if an
# author already declared one.
_KNOWN_FORGE_PROTOCOLS = ("atlas_research", "atlas_capture", "raw_text")
_VALID_ON_FAILURE = ("post_fallback", "silent")


def _extract_prose_script_path(bot_guidance: Any) -> str | None:
    """Return the first ``scripts/<name>.py`` path mentioned in bot_guidance prose."""
    if not isinstance(bot_guidance, list):
        return None
    for section in bot_guidance:
        if not isinstance(section, dict):
            continue
        content = section.get("content")
        if not isinstance(content, str):
            continue
        m = _PROSE_SCRIPT_RE.search(content)
        if m:
            return m.group(1)
    return None


def _invokes_script_consistent(invokes: Any, script: Any) -> bool:
    """Mirror the validator's invokes ↔ script basename check.

    Returns True when the pair would NOT trip
    ``bot_guidance_freelance_validator``'s consistency error (either field
    absent → no check; otherwise the script basename must reference the
    invokes logical_name).
    """
    if not invokes or not script or not isinstance(script, str):
        return True
    basename = script.rsplit("/", 1)[-1]
    stem = basename.rsplit(".", 1)[0]
    return invokes == stem or invokes in basename


def _build_trigger_invocation(
    trigger: dict, prose_script: str | None,
) -> dict | None:
    """Synthesize a complete, validator-clean ``invocation`` contract for one
    event_triggers[] entry, preserving any author-set fields.

    Returns None when the trigger can't be made plugin_intercept-ready — no
    compilable ``match.pattern`` or no resolvable script path. The caller
    treats a single None as "leave the whole manifest at agent_invokes", since
    plugin_intercept demands a valid invocation on *every* trigger.
    """
    if not isinstance(trigger, dict):
        return None

    # Layer C matches on match.pattern; the validator rejects a broken one and
    # the plugin drops a missing one. Require a present, compilable pattern.
    match = trigger.get("match")
    if not isinstance(match, dict):
        return None
    pattern = match.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return None
    try:
        re.compile(pattern)
    except re.error:
        return None
    exclude = match.get("exclude_pattern")
    if isinstance(exclude, str) and exclude:
        try:
            re.compile(exclude)
        except re.error:
            return None

    existing = trigger.get("invocation")
    existing = existing if isinstance(existing, dict) else {}
    invokes = trigger.get("invokes")

    # ── Resolve the script path ──────────────────────────────────────────────
    # Priority: author-set invocation.script → prose-mined path (only when it
    # stays consistent with `invokes`, so we never introduce a validator
    # error) → invokes-derived scripts/<invokes>.py (always consistent).
    script = None
    cand = existing.get("script")
    if isinstance(cand, str) and cand:
        script = cand
    elif prose_script and _invokes_script_consistent(invokes, prose_script):
        script = prose_script
    elif isinstance(invokes, str) and invokes:
        script = f"scripts/{invokes}.py"
    elif prose_script:
        script = prose_script
    if not script:
        return None

    # ── stdout_protocol: keep a known author choice; else generic raw_text ───
    protocol = existing.get("stdout_protocol")
    if not (isinstance(protocol, str) and protocol in _KNOWN_FORGE_PROTOCOLS):
        protocol = "raw_text"

    # ── on_failure + fallback_text ───────────────────────────────────────────
    on_failure = existing.get("on_failure")
    if on_failure not in _VALID_ON_FAILURE:
        on_failure = "post_fallback"
    fallback_text = existing.get("fallback_text")
    if not (isinstance(fallback_text, str) and fallback_text):
        fallback_text = ""
    if on_failure == "post_fallback" and not fallback_text:
        fallback_text = _DEFAULT_FREELANCE_FALLBACK

    # ── request file plumbing (the plugin always passes a JSON request arg) ──
    request_file_template = existing.get("request_file_template")
    if not (isinstance(request_file_template, str) and request_file_template):
        request_file_template = "/tmp/forge-trigger-{message_id}.json"
    request_payload = existing.get("request_payload")
    if not isinstance(request_payload, dict) or not request_payload:
        request_payload = {
            "message_text": "{message_text}",
            "from_id": "{from_id}",
            "message_id": "{message_id}",
            "chat_id": "{chat_id}",
            "chat_type": "{chat_type}",
        }

    return {
        "script": script,
        "request_file_template": request_file_template,
        "request_payload": request_payload,
        "stdout_protocol": protocol,
        "on_failure": on_failure,
        "fallback_text": fallback_text,
    }


def _normalize_freelance_invocation(manifest: ApplicationManifest) -> bool:
    """Upgrade a forged at-risk script app off the leaky agent_invokes default.

    If ``bot_guidance`` routes a chat trigger through a bot-local script
    (detected via the validator's own at-risk-shaped heuristic), the manifest
    still carries the default ``invocation_mode='agent_invokes'``, and every
    ``event_triggers[]`` entry can be made plugin_intercept-ready, then:

      * set ``invocation_mode='plugin_intercept'`` (Layer C structural
        enforcement instead of trusting the LLM), and
      * synthesize a complete ``invocation`` contract on each trigger.

    Returns True iff the manifest was modified. No-op (returns False) when:

      * bot_guidance shows no at-risk markers (a non-script app),
      * invocation_mode is already non-default (an operator/gallery package
        explicitly opted in or out — don't second-guess it),
      * there are no event_triggers[] to attach a contract to (plugin_intercept
        requires a non-empty trigger list; the agent_invokes + at-risk +
        no-triggers posture is the validator's info-level case, left as-is), or
      * any trigger can't be made contract-clean (no compilable pattern / no
        resolvable script) — a partial conversion would just trip the
        validator's build_blocker, so leaving agent_invokes is the safe
        status quo.

    Reuses ``bot_guidance_freelance_validator._is_at_risk_shaped`` for
    detection rather than re-implementing the prose-marker scan.
    """
    # Only touch the leaky default. plugin_intercept (already opted in) or
    # subagent (reserved) are deliberate author choices.
    current_mode = getattr(manifest, "invocation_mode", None) or "agent_invokes"
    if current_mode != "agent_invokes":
        return False

    from .bot_guidance_freelance_validator import _is_at_risk_shaped

    bot_guidance = getattr(manifest, "bot_guidance", None) or []
    is_at_risk, _markers = _is_at_risk_shaped(bot_guidance)
    if not is_at_risk:
        return False

    triggers = getattr(manifest, "event_triggers", None)
    if not isinstance(triggers, list) or not triggers:
        return False

    prose_script = _extract_prose_script_path(bot_guidance)

    # Build every contract first; commit only if ALL triggers convert cleanly.
    built: list[dict] = []
    for trigger in triggers:
        contract = _build_trigger_invocation(trigger, prose_script)
        if contract is None:
            return False
        built.append(contract)

    for trigger, contract in zip(triggers, built):
        trigger["invocation"] = contract
    manifest.invocation_mode = "plugin_intercept"
    return True


def _bake_result_harness(
    job: ForgeJob,
    manifest: ApplicationManifest,
    shared_dir: Path,
) -> None:
    """Bake the execution-integrity floor into this forged app.

    Two parts, both idempotent across re-forges:

      1. **Wrapper file** — write the self-reporting launcher
         (``app_result.WRAPPER_SOURCE``) into the bot's workspace at
         ``evolve/app_result/run.py``. App-agnostic: one launcher per bot wraps
         any app command. Under ``workspace/evolve/`` (evolve-write ACL), so no
         bot-owned-file write — this bite does NOT depend on the privileged-write
         helper, and so covers newly forged / re-forged apps only.
      2. **bot_guidance** — merge the honesty/usage section so the assistant
         runs app commands through the wrapper and reports from the structured
         result (never claiming success on a ``failed`` / absent block).

    Spec docs/spec-app-invocation-just-works-2026-06-29.md §2.2; OQ-2 re-resolved
    to the forge-baked floor in docs/blocker-app-integrity-harness-oq2-2026-06-30.md.

    Best-effort: any failure logs and returns. The harness is a hardening floor,
    not an apply gate — a write/permission hiccup must never block an otherwise
    valid forge.
    """
    from . import app_result

    # Part 2 first (pure in-memory; cannot fail the apply): merge the section so
    # the manifest the 5c bot_guidance gate validates already carries it.
    try:
        manifest.bot_guidance = app_result.merge_bot_guidance(manifest.bot_guidance)
    except Exception as exc:  # noqa: BLE001
        _append_log(
            job.job_id, shared_dir,
            f"Result harness: bot_guidance merge skipped (non-fatal): {exc}",
        )

    # Part 1: write the launcher into the bot workspace.
    try:
        ws_root = _resolve_workspace_root(job.bot_id)
        dest = app_result.write_wrapper(ws_root)
        _append_log(
            job.job_id, shared_dir,
            f"Result harness: wrapper ensured at {dest}",
        )
    except Exception as exc:  # noqa: BLE001
        _append_log(
            job.job_id, shared_dir,
            f"Result harness: wrapper write skipped (non-fatal): {exc}",
        )


def _apply_forge_output(
    job: ForgeJob,
    manifest: ApplicationManifest,
    shared_dir: Path,
    new_pkg_version: str,
    api_key: str = "",
    builder_model: str = _DEFAULT_BUILDER_MODEL,
) -> None:
    """
    Phase 5 manifest finalisation.

    The actual file writing was performed by the bot's LLM session during Phase 1/2.
    This function finalises the manifest state after approval:
        - status        → "approved"
        - pkg_version   → new_pkg_version
        - last_reviewed → today's ISO date
        - source        → set for install jobs; preserved for improvement/update/hotfix
        - source_detail → forge job reference for all job types

    Forge-time test gate removed 2026-06-08 — app-test surface killed
    per docs/decision-app-tests-2026-06-08.md. Coherence gate (below) is
    still the load-bearing forge-approval barrier.
    """
    from datetime import date

    manifest.status        = "approved"
    manifest.pkg_version   = new_pkg_version
    manifest.last_reviewed = date.today().isoformat()

    # ── audit_eligible determination ─────────────────────────────────────────
    # Spec §6.1: forge decides whether an app is worth auto-auditing based on
    # its shape. Apps with no executable code (pure-data manifests, static
    # reference docs) are marked ineligible — auto-audits skip them, but
    # manual audits (CLI / UI / evo) still run regardless. Operators can
    # override either way in the manifest editor.
    # We only set this on FIRST forge approval — preserves operator overrides
    # on subsequent rebuilds.
    if not manifest.last_audit and manifest.audit_eligible is True:
        manifest.audit_eligible = _derive_audit_eligibility(manifest)

    # ── Provenance stamping ───────────────────────────────────────────────────
    # source is the origin story — only set it when the forge run IS the origin
    # (install jobs).  Improvement/update/hotfix runs leave source alone because
    # the manifest already has a meaningful origin value.
    if job.job_type == "install":
        manifest.source = MANIFEST_SOURCE_GALLERY
        manifest.source_detail = (
            f"gallery:{job.gallery_version or 'unknown'}:job:{job.job_id}"
        )
        # v27 born-status: a fresh forge/gallery install is an explicit
        # operator act of creation, so the app is born "defined" (the
        # source of truth). Only on install — improvement/update/hotfix runs
        # (the else branch) operate on an existing manifest and must NOT
        # silently promote it; they leave definition_status untouched so a
        # bot-improved "discovered" app stays churnable until an operator
        # promotes it. See manifest.born_definition_status (§9).
        manifest.definition_status = born_definition_status(manifest.source)
    else:
        # For all non-install runs: if the manifest has no meaningful origin yet
        # (e.g. the bot wrote it from scratch outside a gallery install, so it only
        # carries the default "discovered" value), promote it to bot_created.
        if manifest.source in ("", MANIFEST_SOURCE_DISCOVERED):
            manifest.source = MANIFEST_SOURCE_BOT_CREATED
        # Always update source_detail so operators can trace which forge run
        # produced the current approved state.
        manifest.source_detail = f"forge:{job.job_type}:job:{job.job_id}"

    # Write back the interface_contract extracted after the build/critique cycle.
    # This is the authoritative record of what forge actually built — field names,
    # CLI signatures, file paths — used as context when dependent apps are built.
    extracted_contract = job.context_snapshot.get("interface_contract")
    if extracted_contract and isinstance(extracted_contract, dict):
        manifest.interface_contract = extracted_contract
        _append_log(
            job.job_id, shared_dir,
            "Phase 5: writing extracted interface_contract to manifest "
            f"(extracted_at={extracted_contract.get('extracted_at', '?')})"
        )

    # ── Execution-integrity floor (forge-baked self-reporting harness) ────────
    # Bake the result wrapper + honesty bot_guidance before the gates below so
    # the 5c bot_guidance validator sees the final manifest. Best-effort inside.
    _bake_result_harness(job, manifest, shared_dir)

    # ── Spec reconciliation ───────────────────────────────────────────────────
    # Rewrite build_spec to match what was actually built.  This is vital: the
    # build_spec is the starting point for every future improvement run, so it
    # must reflect the real implementation, not the original intent.
    final_impl = job.context_snapshot.get("final_impl", "")
    if final_impl and api_key:
        original_spec = manifest.build_spec or ""
        reconciled = _reconcile_build_spec(
            final_impl     = final_impl,
            original_spec  = original_spec,
            job            = job,
            shared_dir     = shared_dir,
            api_key        = api_key,
            model          = builder_model,
        )
        if reconciled and reconciled != original_spec:
            manifest.build_spec = reconciled
            _append_log(job.job_id, shared_dir, "Phase 5: build_spec reconciled with final implementation")
    elif not api_key:
        _append_log(job.job_id, shared_dir,
                    "Phase 5: skipping spec reconciliation (no api_key available)")

    # ── Phase 5b: re-validate scheduled_actions coverage post-reconciliation ─
    # build_spec reconciliation can introduce daemon/cron prose that wasn't
    # in the original (e.g. an improvement run that re-frames the app as
    # daemon-driven). If that happens without a matching scheduled_actions[]
    # entry, the rest of this phase will save a manifest that ships with
    # no cron installed — same shape as the Atlas 2026-06-04 incident, but
    # via the improvement-loop path rather than first-install. Fail loudly.
    try:
        from .scheduled_actions_validator import validate_scheduled_actions
        sa_result = validate_scheduled_actions(manifest)
    except Exception as _sa_exc:  # noqa: BLE001
        _append_log(
            job.job_id, shared_dir,
            f"Phase 5b: scheduled_actions validator unavailable (non-fatal): {_sa_exc}",
        )
        sa_result = None

    if sa_result and not sa_result["ok"]:
        err = (
            f"Phase 5b: scheduled_actions gate refused post-build manifest — "
            f"{sa_result['message']}"
        )
        _append_log(job.job_id, shared_dir, err)
        # Raise so ``approve_forge_job``'s outer try/except handles step
        # marking + status transition uniformly with every other apply
        # failure mode. The manifest is NOT saved (we abort before the
        # save call below).
        raise RuntimeError(err)

    # ── Phase 5c.0: default at-risk script apps to plugin_intercept ──────────
    # Before the gate below validates the invocation contract, upgrade a forged
    # app that routes a chat trigger through a bot-local script off the leaky
    # 'agent_invokes' default — synthesizing the invocation{} contract Layer C
    # needs so the plugin intercepts the trigger instead of trusting the LLM
    # (spec docs/spec-agent-freelance-bypass-phase2-2026-06-06.md). No-op for
    # non-script apps and for manifests an author already opted in/out of.
    try:
        if _normalize_freelance_invocation(manifest):
            _append_log(
                job.job_id, shared_dir,
                "Phase 5c.0: at-risk script app upgraded to "
                "invocation_mode='plugin_intercept' with synthesized "
                f"invocation contract on {len(manifest.event_triggers)} "
                "trigger(s) — Layer C will intercept instead of the LLM.",
            )
    except Exception as _norm_exc:  # noqa: BLE001
        # Normalization is best-effort hardening; a failure must never block an
        # otherwise-valid apply. The manifest stays at agent_invokes (status
        # quo) and the gate below validates it as-is.
        _append_log(
            job.job_id, shared_dir,
            f"Phase 5c.0: invocation-mode normalization skipped (non-fatal): "
            f"{_norm_exc}",
        )

    # ── Phase 5c: bot_guidance freelance-bypass gate (post-reconciliation) ───
    # Same rationale as Phase 5b but for the agent-freelance-bypass
    # invocation contract. A reconciliation pass may introduce
    # invocation_mode='plugin_intercept' or modify event_triggers[].invocation;
    # if the rebuilt manifest's contract is broken, fail loudly before
    # the manifest save the same way scheduled_actions does.
    try:
        from .bot_guidance_freelance_validator import validate_bot_guidance
        bg_result = validate_bot_guidance(manifest)
    except Exception as _bg_exc:  # noqa: BLE001
        _append_log(
            job.job_id, shared_dir,
            f"Phase 5c: bot_guidance validator unavailable (non-fatal): {_bg_exc}",
        )
        bg_result = None

    if bg_result and not bg_result["ok"]:
        err = (
            f"Phase 5c: bot_guidance gate refused post-build manifest — "
            f"{bg_result['message']}"
        )
        _append_log(job.job_id, shared_dir, err)
        raise RuntimeError(err)

    # ── Phase 5d: apps-inherit-bot-llm gate (post-reconciliation) ────────────
    # Refuses manifests whose ``recursive_llm`` block declares per-app
    # credentials, an invalid/missing transport, or a credential template
    # in ``files[]``. See docs/principle-apps-inherit-bot-llm.md and
    # docs/spec-apps-inherit-bot-llm-2026-06-06.md for the full rule.
    # A reconciliation pass that re-introduces the old api_key_source
    # shape (e.g. via an improvement run that pulled a stale Spec) is
    # the failure mode this gate catches.
    try:
        from .apps_inherit_bot_llm_validator import validate_apps_inherit_bot_llm
        aibl_result = validate_apps_inherit_bot_llm(manifest)
    except Exception as _aibl_exc:  # noqa: BLE001
        _append_log(
            job.job_id, shared_dir,
            f"Phase 5d: apps-inherit-bot-llm validator unavailable (non-fatal): {_aibl_exc}",
        )
        aibl_result = None

    if aibl_result and not aibl_result["ok"]:
        err = (
            f"Phase 5d: apps-inherit-bot-llm gate refused post-build manifest — "
            f"{aibl_result['message']}"
        )
        _append_log(job.job_id, shared_dir, err)
        raise RuntimeError(err)

    # ── Phase 5e: privacy{} + audience_scoping{} gate (post-reconciliation) ──
    # Re-validates after spec reconciliation, mirroring Step 1e — a rebuilt
    # manifest that regressed the declared trust boundary (malformed block,
    # trigger audience drifting off the role_capabilities vocabulary, group
    # trigger gaining a surface without a consent notice) fails here before
    # the manifest is saved. Slicing spec §4.1.
    try:
        from .privacy_scoping_validator import validate_privacy_scoping
        ps_result = validate_privacy_scoping(manifest)
    except Exception as _ps_exc:  # noqa: BLE001
        _append_log(
            job.job_id, shared_dir,
            f"Phase 5e: privacy_scoping validator unavailable (non-fatal): {_ps_exc}",
        )
        ps_result = None

    if ps_result and not ps_result["ok"]:
        err = (
            f"Phase 5e: privacy/audience gate refused post-build manifest — "
            f"{ps_result['message']}"
        )
        _append_log(job.job_id, shared_dir, err)
        raise RuntimeError(err)

    # ── Phase 4.5: Materialize scheduled_actions[] (forge install) ───────────
    # Spec: docs/spec-forge-side-effects-2026-06-02.md §5.
    #
    # Walk every scheduled_actions[] entry; for each one whose mechanism is
    # a forge-installable type (oc_*_hook, launchd), invoke the privileged
    # install helper and stamp installed_at/installed_by/installed_artifact
    # so the verifier (PR 3) can resolve the install on the next audit run.
    #
    # Per-action best-effort: a failure on one action records the error but
    # does NOT abort the apply phase. The operator sees the
    # scheduled_actions_installed[] summary on job.context_snapshot and can
    # remediate one entry without reverting the whole forge.
    try:
        install_summary = _materialize_scheduled_actions(job, manifest)
        # Stamp the summary on the job so the operator review surface (and
        # any caller of run_forge_job's return value) can render it.
        job.context_snapshot["scheduled_actions_installed"] = install_summary
        n_failed = sum(1 for e in install_summary if e.get("status") == "failed")
        _append_log(
            job.job_id, shared_dir,
            "Phase 4.5: materialize scheduled_actions — "
            f"installed={sum(1 for e in install_summary if e.get('status') == 'ok')}, "
            f"failed={n_failed}, "
            f"skipped={sum(1 for e in install_summary if e.get('status') == 'skipped')}",
        )
        # Failure visibility (audit slate S2) — manifest stamps +
        # completed_with_errors + per-action Signals. Runs BEFORE the
        # save_manifest below, so the stamps persist.
        _record_scheduled_action_outcomes(
            job, manifest, install_summary, shared_dir,
        )
    except Exception as exc:
        # Belt-and-suspenders: the helper itself catches per-action errors,
        # so a top-level exception here is something structural (import
        # failure, manifest shape bug). Log + continue — manifest save
        # still needs to happen.
        _append_log(
            job.job_id, shared_dir,
            f"Phase 4.5: materialize scheduled_actions skipped (non-fatal): {exc}",
        )

    try:
        save_manifest(manifest, shared_dir)
        _append_log(job.job_id, shared_dir,
                    f"Phase 5: manifest saved (status=approved, pkg_version={new_pkg_version}, "
                    f"source={manifest.source}, source_detail={manifest.source_detail})")
    except Exception as exc:
        _append_log(job.job_id, shared_dir, f"Phase 5: manifest save failed: {exc}")
        raise

    # ── Phase C: post-apply reality check ─────────────────────────────────────
    # Walk every file claim in the just-saved manifest and confirm reality
    # matches: file exists on disk, sha256 (if recorded) matches, file
    # readable. Result lands in manifest.last_verification — surfaced to the
    # operator via the Apps page so phantom file records don't ship silently.
    #
    # Non-fatal: even if verification finds problems, we don't reverse the
    # approval (that needs rollback work — see Phase D follow-ups). We
    # surface the result so the operator can decide to re-forge, manually
    # repair, or accept.
    try:
        verify_report = bot_forge.verify_manifest_reality(manifest, job.bot_id)
        manifest.last_verification = verify_report
        save_manifest(manifest, shared_dir)
        _append_log(
            job.job_id, shared_dir,
            f"Phase 5: verification {verify_report['status']}: "
            f"{verify_report['summary']}",
        )
        if verify_report["status"] == "failed":
            _append_log(
                job.job_id, shared_dir,
                f"Phase 5: VERIFICATION FAILED — manifest claims files that "
                f"don't exist on disk: "
                f"{verify_report['files']['missing'][:5]}",
            )
    except Exception as exc:
        _append_log(
            job.job_id, shared_dir,
            f"Phase 5: post-apply verification skipped (non-fatal): {exc}",
        )

    # ── Phase 5a.0: infer usage.model if missing ─────────────────────────────
    # Before running the discoverability check, fill in usage.model from
    # structural cues if the author didn't set it. Deterministic
    # (scheduled_actions → "scheduled"; event_triggers → "event-driven";
    # cli → "user-initiated"; else "user-initiated"). Eliminates the
    # no_invocation_model finding for every install where the author wasn't
    # aware of the field — the most common gap on the gallery's pre-contract
    # packages, and the one the LLM-authored Spec path also tends to skip.
    # See app_registry.infer_usage_model for the rule set.
    try:
        from . import app_registry as _ar
        current_model = ""
        if isinstance(manifest.usage, dict):
            current_model = str(manifest.usage.get("model") or "").strip()
        if not current_model:
            inferred = _ar.infer_usage_model(manifest.to_dict())
            if not isinstance(manifest.usage, dict):
                manifest.usage = {}
            manifest.usage["model"] = inferred
            save_manifest(manifest, shared_dir)
            _append_log(
                job.job_id, shared_dir,
                f"Phase 5a.0: usage.model inferred → {inferred!r} "
                f"(author left it unset; inferred from manifest structure)",
            )
    except Exception as exc:
        _append_log(
            job.job_id, shared_dir,
            f"Phase 5a.0: usage.model inference skipped (non-fatal): {exc}",
        )

    # ── Phase 5a: discoverability warn ───────────────────────────────────────
    # Run the same check_discoverability assertion that the Tier-2 audit
    # uses, but at apply time so the operator sees gaps in the job log
    # before the app goes live. Non-blocking — apply still completes, and
    # the audit will re-fire the same assertions in the next 6-hour sweep
    # if the gaps remain.
    #
    # The check mirrors render_installed_apps_md's contract: if the bot's
    # LLM would read a thin entry, this fires. Connects to the
    # agent-freelance-bypass class of failures — apps the LLM can't route
    # to get freelanced past, bypassing scope/grounding/privacy controls.
    # See docs/manifest-authoring-guide.md §5.22.
    try:
        from app_audit_structural import check_discoverability  # noqa: E402

        disc_findings = check_discoverability(manifest.to_dict(), {})
        if disc_findings:
            summary = [
                {
                    "assertion_id": f.assertion_id,
                    "severity": f.severity,
                    "summary": f.summary,
                }
                for f in disc_findings
            ]
            job.context_snapshot["discoverability_warnings"] = summary
            for f in disc_findings:
                _append_log(
                    job.job_id, shared_dir,
                    f"Phase 5a: discoverability {f.severity} — "
                    f"{f.assertion_id}: {f.summary}",
                )
            _append_log(
                job.job_id, shared_dir,
                f"Phase 5a: {len(disc_findings)} discoverability gap(s) — "
                f"see manifest-authoring-guide.md §5.22 "
                f"(app will install but the bot's LLM may not route to it)",
            )
        else:
            _append_log(
                job.job_id, shared_dir,
                "Phase 5a: discoverability check passed",
            )
    except Exception as exc:
        _append_log(
            job.job_id, shared_dir,
            f"Phase 5a: discoverability check skipped (non-fatal): {exc}",
        )

    # ── Phase 5b: regenerate bot-side INSTALLED_APPS.md ──────────────────────
    # The bot's LLM reads this file at session start to know what apps it
    # has and how to invoke them. Regenerating here means a fresh install
    # is conversationally usable from the next turn forward, not just
    # technically present on disk.
    try:
        from . import app_registry as _ar
        out_path = _ar.regenerate_installed_apps_md(job.bot_id, shared_dir)
        if out_path is not None:
            _append_log(
                job.job_id, shared_dir,
                f"Phase 5: INSTALLED_APPS.md regenerated at {out_path}",
            )
    except Exception as exc:
        _append_log(
            job.job_id, shared_dir,
            f"Phase 5: INSTALLED_APPS.md regenerate skipped (non-fatal): {exc}",
        )


# ── Top-level entry points ────────────────────────────────────────────────────

def run_forge_job(
    job_id: str,
    shared_dir: Path,
    bot_id: str,
    auto_approve_actor: str | None = None,
) -> None:
    """
    Top-level entry point for a forge run.  Called by the bot agent session.

    Executes Phases 1–4:
        Step 1  — inject manifest / set status → updating
        Step 2  — Phase 1: initial build
        Steps 3–6 — Phase 2: critique cycle (rounds × 2 steps)
        Step 7  — Phase 3: no-op (was test_command; surface removed 2026-06-08)
        Step 8  — mark awaiting_approval + notify operator (skipped when
                  ``auto_approve_actor`` is set — see below)

    When ``auto_approve_actor`` is provided (e.g. for messaging-driven
    builds where the design conversation served as the gate), Step 8 is
    skipped and ``approve_forge_job`` is called directly with the given
    actor. The user-side gate already happened in chat; forge runs to
    completion autonomously. See spec-forge-via-messaging-2026-05-07.md
    for the full justification.

    On any phase failure: marks the step as failed and sets job status → failed.
    """
    job = load_job(job_id, shared_dir)
    if job is None:
        raise ValueError(f"forge_engine: job {job_id!r} not found")

    if job.status not in ("queued", "running"):
        raise ValueError(
            f"forge_engine: job {job_id!r} is in state {job.status!r}, cannot run"
        )

    _append_log(job_id, shared_dir,
                f"run_forge_job starting: type={job.job_type} app={job.app_id} bot={job.bot_id}")

    # ── Auto-ship for install jobs ────────────────────────────────────────────
    # First-time installs don't benefit from an operator approval gate:
    #   - There's no prior version to diff against (no "what changed?")
    #   - The approval modal renders ``(not available)`` for installs
    #     because the context_snapshot doesn't carry final_impl /
    #     interface_contract; nothing to actually review
    #   - The operator's "did I want this app?" decision happened earlier
    #     in the gallery install click; gating on a second decision adds
    #     no signal
    #   - Tests already passed (we don't reach Step 8 otherwise) + critique
    #     converged
    # Net: installs ship automatically; the operator can audit via the
    # manifest button (Forge Jobs page) and freely edit / reinstall later.
    # Improvement jobs KEEP the approval gate — they have a real before/
    # after to review and a meaningful "approve or revert" decision.
    #
    # auto_approve_actor explicitly set by the caller (messaging-driven
    # builds) still wins — this is an additional auto-trigger, not a
    # replacement.
    if auto_approve_actor is None and job.job_type == "install":
        auto_approve_actor = "forge_install_auto"
        _append_log(
            job_id, shared_dir,
            "Install job detected — will auto-ship after test (no approval gate)",
        )

    # ── Provisioning budget gate (decision B) ────────────────────────────────
    # An install job is app PROVISIONING. Before seeding the manifest or
    # dispatching any build, check the bot's one-time provisioning ceiling +
    # daily cost breaker. If the standup has already spent its ceiling (or the
    # daily cap breaker is tripped), refuse THIS build before a token is billed
    # and mark the job failed with a budget reason. Because the starter-pack
    # installer runs jobs sequentially, refusing here gives clean complete-
    # current-then-stop: apps already built stay complete, the next is paused.
    # A Signal is emitted inside the helper so the cap is observable, never a
    # silently half-provisioned bot. Finding:
    # docs/finding-new-bot-activation-cost-2026-06-12.md (decision B).
    if job.job_type == "install":
        budget_decision = _provisioning_budget_decision(job, shared_dir)
        if budget_decision is not None:
            _append_log(
                job_id, shared_dir,
                f"Provisioning paused before build: {budget_decision.reason}. "
                f"No LLM cost incurred; apps already built are unaffected. "
                f"Raise the ceiling / reset the breaker and retry to resume.",
            )
            mark_step_failed(job, 1, budget_decision.reason, shared_dir)
            return

    # Resolve API key and model config once.
    #
    # Bot-driven forge (Phases 1-4: build/critique/refine/test) doesn't need
    # admin's API key — the bot uses its own openclaw credentials via
    # `openclaw agent --local`. Admin's key is only needed at approval time
    # for spec reconciliation (`_reconcile_build_spec`) and interface
    # extraction. We resolve here for those downstream uses but don't block
    # the run when missing: the downstream calls degrade gracefully.
    api_key = _resolve_api_key(bot_id)
    if not api_key:
        _append_log(
            job_id, shared_dir,
            "Note: no admin-side Anthropic API key resolved; "
            "spec reconciliation + interface extraction will be skipped. "
            "Bot-driven build/critique/refine use the bot's own credentials.",
        )

    builder_model, critic_model = _get_models(shared_dir, bot_id=job.bot_id)
    builder_prompt, critic_prompt = _get_prompts(shared_dir)

    # ── Step 1: seed manifest from gallery (install) or mark updating ─────────
    # For fresh installs the per-bot manifest doesn't exist yet — Step 1 writes
    # it now, seeded from the gallery package, so the rest of the forge run
    # (build dispatch, critique, test, Step 10 apply) has something to read,
    # update, and finalise. Without this seed Step 10 fails with
    # "manifest not found" after the whole build has already burned tokens.
    #
    # Seed failure is FATAL at Step 1 (no silent fallthrough). The most common
    # cause is a missing /Users/<bot>/.openclaw/workspace/manifests/ dir on a
    # freshly-created bot — fixed by `sudo evolve-admin deploy <bot>`, which
    # re-runs set_evolve_read_acl + creates the dir with the right ACL.
    mark_step_running(job, 1, shared_dir)
    try:
        manifest = load_manifest(job.app_id, job.bot_id, shared_dir)

        if manifest is None and job.job_type == "install" and job.pkg_id:
            # Fresh install — seed from the gallery package.
            from .gallery import load_gallery_package

            pkg = load_gallery_package(job.pkg_id, shared_dir)
            if pkg is None:
                err = (
                    f"Step 1 failed: gallery package {job.pkg_id!r} not found "
                    f"(searched builtin + imported)"
                )
                _append_log(job_id, shared_dir, err)
                mark_step_failed(job, 1, err, shared_dir)
                return

            seed = dict(pkg)  # don't mutate the gallery copy
            # Per-bot identifiers. The gallery package's own `id` (e.g.
            # "app_journal") may differ from the slug-derived `app_id` the
            # gallery route assigned to this job (e.g. "journal"). The job's
            # app_id is the URL key and the load_manifest lookup key — we
            # force the manifest id to match so the file lands at
            # /Users/<bot>/.openclaw/workspace/manifests/<job.app_id>.json.
            seed["bot_id"] = job.bot_id
            seed["id"] = job.app_id
            seed["status"] = "updating"
            seed["files"] = []
            seed["install_job"] = job.job_id
            seed["source"] = MANIFEST_SOURCE_GALLERY
            seed["source_detail"] = (
                f"gallery:{job.gallery_version or 'unknown'}:job:{job.job_id}"
            )

            # Stamp per-bot default classification tier from network.json.
            # No-op when no default is set, when the default is
            # ``some_data_local`` (template is a no-op), or when the gallery
            # seed already declares classification fields. Never blocks
            # install — if stamping fails, log and continue with the
            # unstamped seed.
            try:
                from data_classification import stamp_per_bot_default  # type: ignore
                from ..config import load_network as _load_network
                seed = stamp_per_bot_default(
                    seed, bot_id=job.bot_id, network=_load_network(),
                )
            except Exception as exc:  # noqa: BLE001
                _append_log(
                    job_id, shared_dir,
                    f"Per-bot default-tier stamp skipped: {exc}",
                )

            # v24: every newly built app carries declared privacy{} +
            # audience_scoping{} blocks (manifest-v7 Slice 2 — "Build
            # authors both blocks for new apps"). Gallery packages that
            # declare their own win; otherwise stamp the canonical
            # conservative defaults (collect-nothing, operator-only) —
            # the same inference the v7 migration writes, so new and
            # migrated artifacts agree. Best-effort: a stamp failure
            # must not block the install (the manifest stays in the
            # "not yet declared" posture, which is valid).
            #
            # Seed overrides: jobs queued by the add-bot wizard carry the
            # operator's consent answers in ``context_snapshot``
            # (``privacy_seed`` / ``audience_scoping_seed`` — delta spec
            # §7 + the v24 update). They fill the defaults' gaps so the
            # installed app's manifest says what the conversation
            # decided; a gallery-declared block still wins outright.
            try:
                from .privacy_scoping_validator import (
                    seed_privacy_scoping_defaults,
                )
                snapshot = job.context_snapshot or {}
                priv_seed = snapshot.get("privacy_seed")
                scope_seed = snapshot.get("audience_scoping_seed")
                seed_privacy_scoping_defaults(
                    seed,
                    privacy_overrides=(
                        priv_seed if isinstance(priv_seed, dict) else None
                    ),
                    audience_scoping_override=(
                        scope_seed if isinstance(scope_seed, dict) else None
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                _append_log(
                    job_id, shared_dir,
                    f"privacy/audience default stamp skipped: {exc}",
                )

            # Surface the consent notice in the job log so the operator
            # reviewing the install sees exactly what the app's audience
            # will be told (slicing spec §4.1; the gallery preflight shows
            # the same text pre-dispatch).
            _seed_privacy = seed.get("privacy")
            if isinstance(_seed_privacy, dict):
                _consent = (_seed_privacy.get("consent_notice") or "").strip() \
                    if isinstance(_seed_privacy.get("consent_notice"), str) else ""
                if _consent:
                    _append_log(
                        job_id, shared_dir,
                        f"Step 1: consent notice (shown to this app's "
                        f"audience): {_consent}",
                    )

            # Materialise + save. Failure here is fatal — the rest of the
            # forge run depends on the manifest existing on disk.
            try:
                manifest = ApplicationManifest.from_dict(seed)
                save_manifest(manifest, shared_dir)
            except Exception as exc:
                err = (
                    f"Step 1 failed: could not seed manifest from gallery pkg "
                    f"{job.pkg_id!r} for bot {job.bot_id!r}: {exc}. "
                    f"If this is a freshly-created bot, run "
                    f"`sudo evolve-admin deploy {job.bot_id}` to ensure "
                    f"workspace/manifests/ exists with the evolve write ACL."
                )
                _append_log(job_id, shared_dir, err)
                mark_step_failed(job, 1, err, shared_dir)
                return

            _append_log(
                job_id, shared_dir,
                f"Step 1: seeded manifest from gallery pkg {job.pkg_id}",
            )

        elif manifest is not None:
            # Existing manifest — flip status to updating and (for the rare
            # re-install case) stamp gallery provenance.
            manifest.status = "updating"
            if job.job_type == "install" and manifest.source != MANIFEST_SOURCE_GALLERY:
                manifest.source = MANIFEST_SOURCE_GALLERY
                manifest.source_detail = (
                    f"gallery:{job.gallery_version or 'unknown'}:job:{job.job_id}"
                )
            save_manifest(manifest, shared_dir)
        # else (improvement on a missing manifest): the improvement endpoint
        # validates the manifest exists, so reaching this branch is a
        # caller-side bug. Step 2 will fail naturally if it happens.

        mark_step_done(job, 1, "Manifest seeded / status → updating", shared_dir)
        _append_log(job_id, shared_dir, "Step 1 done")
    except Exception as exc:
        err = f"Step 1 failed: {exc}"
        _append_log(job_id, shared_dir, err)
        mark_step_failed(job, 1, err, shared_dir)
        return

    # ── Step 1b: scheduled_actions coverage gate ─────────────────────────────
    # If the manifest's build_spec narratively describes a daemon/cron but
    # scheduled_actions[] declares no forge-installable mechanism, refuse
    # the run BEFORE Step 2 burns any tokens. The forge would otherwise
    # complete cleanly with the bot's workspace populated but no cron
    # scheduled — the Atlas Daily Digest failure mode (2026-06-04).
    #
    # The gallery preflight at ``gallery.preflight_check`` is the friendly
    # front door, but it can be bypassed via ``force=true`` and isn't
    # re-run after the OAuth wait → dispatch path. This is the durable
    # backstop covering both.
    try:
        from .scheduled_actions_validator import validate_scheduled_actions
        sa_result = validate_scheduled_actions(manifest)
    except Exception as _sa_exc:  # noqa: BLE001
        # Validator unavailable (import failure during refactor, etc.)
        # must not silently let bad installs through, but it also can't
        # block a working install. Log and continue — the gallery-side
        # check has likely already caught real cases.
        _append_log(
            job_id, shared_dir,
            f"Step 1b: scheduled_actions validator unavailable (non-fatal): {_sa_exc}",
        )
        sa_result = None

    if sa_result and not sa_result["ok"]:
        err = (
            f"Step 1b: scheduled_actions gate refused install — {sa_result['message']}"
        )
        _append_log(job_id, shared_dir, err)
        mark_step_failed(job, 1, err, shared_dir)
        return

    # ── Step 1c: bot_guidance freelance-bypass gate ─────────────────────────
    # docs/spec-agent-freelance-bypass-phase2-2026-06-06.md. If the
    # manifest opts into invocation_mode='plugin_intercept' but its
    # event_triggers[].invocation contract is broken (missing fields,
    # uncompilable regex, unknown stdout protocol), refuse the run.
    # Without this gate, install would silently produce a manifest that
    # crashes the plugin's trigger interceptor or falls through to LLM
    # freelance — exactly what Layer C is meant to close.
    try:
        from .bot_guidance_freelance_validator import validate_bot_guidance
        bg_result = validate_bot_guidance(manifest)
    except Exception as _bg_exc:  # noqa: BLE001
        _append_log(
            job_id, shared_dir,
            f"Step 1c: bot_guidance validator unavailable (non-fatal): {_bg_exc}",
        )
        bg_result = None

    if bg_result and not bg_result["ok"]:
        err = (
            f"Step 1c: bot_guidance gate refused install — {bg_result['message']}"
        )
        _append_log(job_id, shared_dir, err)
        mark_step_failed(job, 1, err, shared_dir)
        return

    # ── Step 1d: apps-inherit-bot-llm gate ──────────────────────────────────
    # docs/spec-apps-inherit-bot-llm-2026-06-06.md. Refuses installs whose
    # manifest declares per-app LLM credentials (`api_key_source`), an
    # invalid or missing `transport`, or a credential template in
    # `files[]`. Catches the Atlas regression class at the entry boundary
    # so it can't recur via a pasted-spec or improvement-run path.
    try:
        from .apps_inherit_bot_llm_validator import validate_apps_inherit_bot_llm
        aibl_result = validate_apps_inherit_bot_llm(manifest)
    except Exception as _aibl_exc:  # noqa: BLE001
        _append_log(
            job_id, shared_dir,
            f"Step 1d: apps-inherit-bot-llm validator unavailable (non-fatal): {_aibl_exc}",
        )
        aibl_result = None

    if aibl_result and not aibl_result["ok"]:
        err = (
            f"Step 1d: apps-inherit-bot-llm gate refused install — {aibl_result['message']}"
        )
        _append_log(job_id, shared_dir, err)
        mark_step_failed(job, 1, err, shared_dir)
        return

    # ── Step 1e: privacy{} + audience_scoping{} gate ─────────────────────────
    # Manifest-v7 Slice 2 (docs/spec-manifest-v7-slicing-2026-06-10.md §4.1).
    # Refuses manifests whose declared privacy / audience_scoping block is
    # malformed, whose event_triggers[].audience doesn't name a declared
    # role_capabilities key, or that listen on a group surface without a
    # consent notice. Absent blocks pass — declaring is the opt-in.
    try:
        from .privacy_scoping_validator import validate_privacy_scoping
        ps_result = validate_privacy_scoping(manifest)
    except Exception as _ps_exc:  # noqa: BLE001
        _append_log(
            job_id, shared_dir,
            f"Step 1e: privacy_scoping validator unavailable (non-fatal): {_ps_exc}",
        )
        ps_result = None

    if ps_result and not ps_result["ok"]:
        err = (
            f"Step 1e: privacy/audience gate refused install — {ps_result['message']}"
        )
        _append_log(job_id, shared_dir, err)
        mark_step_failed(job, 1, err, shared_dir)
        return

    # ── Assemble context package ──────────────────────────────────────────────
    try:
        context = assemble_context_package(job, shared_dir)
    except Exception as exc:
        err = f"Context assembly failed: {exc}"
        _append_log(job_id, shared_dir, err)
        mark_step_failed(job, 2, err, shared_dir)
        return

    # Determine critique rounds from manifest constraints / calibration
    manifest_for_rounds = load_manifest(job.app_id, job.bot_id, shared_dir)
    critique_rounds = _get_critique_rounds(
        manifest_for_rounds or ApplicationManifest(id=job.app_id, name=job.app_id, bot_id=bot_id),
        shared_dir,
    )
    _append_log(job_id, shared_dir, f"Critique rounds configured: {critique_rounds}")

    # ── Steps 2–7: bot-driven forge ───────────────────────────────────────────
    # The build, critique, materialize, and test phases are all delegated to
    # the target bot's own LLM via `openclaw agent`. The bot writes files in
    # its own workspace (no admin-side permission gymnastics), stamps
    # provenance markers, runs the test command, and writes a summary to
    # workspace/evolve/forge/outbox/<job_id>.json. We mark all the relevant
    # job steps done from the outbox result.
    try:
        _run_bot_dispatch(job, context, shared_dir, critique_rounds)
    except Exception as exc:
        _append_log(job_id, shared_dir, f"Bot-driven forge failed: {exc}")
        return  # step already marked failed inside _run_bot_dispatch

    # ── Integration check (non-blocking; between Step 7 and Step 8) ───────────
    # See docs/spec-export-import-forge-2026-05-26.md §3.3.
    # Only runs when the manifest declares shared_modules / app_dependencies /
    # recursive_llm. Logs findings; does NOT modify job state.
    try:
        _run_integration_check(job, shared_dir)
    except Exception as exc:  # pragma: no cover — defensive
        _append_log(job_id, shared_dir, f"Integration check raised (non-fatal): {exc}")

    # ── Step 8: mark awaiting approval + notify ───────────────────────────────
    # Bypassed when auto_approve_actor is set (messaging-driven builds);
    # in that case we go straight from Phase 3 (test) → Phase 5 (apply)
    # via approve_forge_job, treating the design conversation as the gate.
    if auto_approve_actor:
        _append_log(
            job_id, shared_dir,
            f"Step 8 auto-approve: marking awaiting then approving as "
            f"{auto_approve_actor!r}",
        )
        try:
            # approve_forge_job() requires job.status == "awaiting_approval"
            # — without this transition, the call fails with "cannot
            # approve" and the job stalls at running/step=7 forever. The
            # mark_awaiting_approval call is a single-line state flip, NOT
            # a separate "wait for operator" semantic — operators see the
            # job go straight from running → approved → complete in the
            # forge log, identical to the messaging-driven flow.
            # See atlas-daily-digest j-a9bc8b38 (2026-05-29) for the regression.
            mark_step_running(job, 8, shared_dir)
            mark_awaiting_approval(job, shared_dir)
            mark_step_done(job, 8, "Status → awaiting_approval (auto)", shared_dir)

            # Notes prose distinguishes the two auto-approve paths so the
            # job history reads truthfully. forge_install_auto = first-time
            # install (no prior version to gate against); other actors =
            # messaging-driven (design conversation served as the gate).
            if auto_approve_actor == "forge_install_auto":
                _notes = (
                    "auto-approved (install: no prior version to diff; "
                    "tests passed + critique converged; operator can audit "
                    "via the manifest button)"
                )
            else:
                _notes = "auto-approved (messaging-driven; design-conversation gate)"
            approve_forge_job(
                job_id, shared_dir,
                approved_by=auto_approve_actor,
                notes=_notes,
            )
            _append_log(
                job_id, shared_dir,
                "run_forge_job complete — auto-approved + applied",
            )
        except Exception as exc:
            err = f"Auto-approve failed: {exc}"
            _append_log(job_id, shared_dir, err)
            # The job is left at the post-test state; an operator could
            # still approve via the dashboard if they want to recover.
        return

    mark_step_running(job, 8, shared_dir)
    try:
        mark_awaiting_approval(job, shared_dir)
        mark_step_done(job, 8, "Status → awaiting_approval", shared_dir)
        _append_log(job_id, shared_dir, "Step 8 done: job awaiting operator approval")
    except Exception as exc:
        err = f"Step 8 failed (mark_awaiting_approval): {exc}"
        _append_log(job_id, shared_dir, err)
        mark_step_failed(job, 8, err, shared_dir)
        return

    try:
        _notify_operator(job, shared_dir)
    except Exception as exc:
        # Non-fatal — log and continue
        _append_log(job_id, shared_dir, f"Operator notification failed (non-fatal): {exc}")

    _append_log(job_id, shared_dir,
                "run_forge_job complete — awaiting operator approval")


def _stamp_connected_messaging_channels(
    manifest: Any, bot_id: str, shared_dir: Path,
) -> list[str]:
    """Declare the bot's LIVE connected messaging channel(s) on a
    manifest that would otherwise trip C-A4 (U1 activation fix,
    2026-06-11 design sync).

    A messaging app's manifest must carry a messaging-capable entry in
    ``requirements.integrations[]`` or the coherence gate refuses it.
    The forge LLM doesn't reliably author that block, and the gallery
    seed can't (a briefing is channel-agnostic until it lands on a
    bot). Stamping the bot's real channel state here means the gate
    verifies reality: a connected bot ships a briefing that declares
    its channel; a channel-less bot still gets the (correct) refusal —
    that refusal is the day-1 channel gap as a manifest invariant, and
    ``briefing_activation`` retries the install when the first channel
    connects.

    Returns the stamped channel ids (empty when nothing applied).
    Best-effort: any failure leaves the manifest as-authored and the
    gate decides.
    """
    try:
        from dataclasses import asdict as _stamp_asdict
        from .coherence_pass_a import manifest_missing_messaging_integration
        from ..channels import enabled_messaging_channels

        if not manifest_missing_messaging_integration(_stamp_asdict(manifest)):
            return []
        connected = sorted(enabled_messaging_channels(bot_id))
        if not connected:
            return []
        if not isinstance(manifest.requirements, dict):
            manifest.requirements = {}
        # Dict entries, not bare strings — the v7-arc integration
        # translation (migrate_v7._build_integrations) drops non-dict
        # entries, and C-A4 accepts both shapes.
        manifest.requirements.setdefault("integrations", []).extend(
            {
                "id": ch,
                "required": True,
                "reason": (
                    "Delivers messages on the bot's connected channel "
                    "(declared from live channel state at install)"
                ),
            }
            for ch in connected
        )
        save_manifest(manifest, shared_dir)
        return connected
    except Exception:  # noqa: BLE001
        log.exception(
            "connected-channel declaration skipped for %s on %s",
            getattr(manifest, "id", "?"), bot_id,
        )
        return []


# Channel preference when a bot is reachable on several — most personal
# first. Mirrors the gallery delivery scripts' own CHANNEL_PRIORITY so the
# channel stamped here matches the one the script resolves at send time.
_DELIVERY_CHANNEL_PRIORITY = (
    "telegram", "whatsapp", "signal", "imessage", "slack",
    "discord", "sms", "matrix",
)


def _preferred_live_channel(connected: set[str]) -> str | None:
    """Pick the most-personal-first channel the bot can actually send on,
    or None when it has no connected messaging channel."""
    for ch in _DELIVERY_CHANNEL_PRIORITY:
        if ch in connected:
            return ch
    return next(iter(sorted(connected)), None)


def _declares_user_facing_delivery(action: Any) -> bool:
    """True if a scheduled_actions[] entry declares a user-facing delivery —
    an ``outputs[]`` entry carrying a channel, or an explicit
    ``delivery_contract.user_facing``. Reads the manifest the same way
    delivery_monitor's ``_derived_user_facing`` / ``effective_contract`` do,
    so "user-facing here" means "monitored there"."""
    if not isinstance(action, dict):
        return False
    dc = action.get("delivery_contract")
    if isinstance(dc, dict) and dc.get("user_facing") is True:
        return True
    for out in action.get("outputs") or []:
        if isinstance(out, dict) and out.get("channel"):
            return True
    return False


def _ensure_monitored_delivery(
    action: dict, spec_action: Any, channel: str,
) -> bool:
    """Make one realized scheduled_actions[] entry visible to the
    proactive-delivery monitor: declare a channel'd ``outputs[]`` entry and
    a ``user_facing`` ``delivery_contract``. Reuses the Spec's contract
    verbatim when it's well-formed, else stamps a minimal scheduler-state
    one. Returns True iff it changed the entry."""
    changed = False

    outputs = action.get("outputs")
    if not isinstance(outputs, list):
        outputs = []
    if not any(isinstance(o, dict) and o.get("channel") for o in outputs):
        action["outputs"] = outputs + [
            {"kind": "session_message", "channel": channel}
        ]
        changed = True

    dc = action.get("delivery_contract")
    if not (isinstance(dc, dict) and dc.get("user_facing") is True):
        spec_dc = (
            spec_action.get("delivery_contract")
            if isinstance(spec_action, dict) else None
        )
        if isinstance(spec_dc, dict) and not validate_delivery_contract(spec_dc):
            new_dc = dict(spec_dc)
            new_dc["user_facing"] = True
        else:
            new_dc = {
                "user_facing": True,
                "window_minutes": 30,
                "evidence": {"ran": {"kind": "scheduler_state"}},
                "heal": "none",
            }
        action["delivery_contract"] = new_dc
        changed = True

    return changed


def _stamp_scheduled_delivery_contracts(
    manifest: Any, bot_id: str, shared_dir: Path,
) -> list[str]:
    """Make user-facing scheduled deliveries visible to the proactive-
    delivery monitor by declaring ``outputs[].channel`` +
    ``delivery_contract`` on the realized manifest, from the bot's LIVE
    channel state (U1 delivery re-proof, 2026-06-12).

    The realized manifest's ``scheduled_actions[]`` are *extracted* from
    the workspace (quality="extracted"); a gallery briefing whose Spec
    declares ``outputs:[{channel}]`` + ``delivery_contract{user_facing}``
    can land with ``outputs:[]`` and no contract, so the monitor's
    ``_derived_user_facing()`` skips it — a *silent* delivery failure, the
    exact gap the monitor exists to catch (ledger, 2026-06-12). Re-assert
    the Spec's delivery intent here, concretizing the channel from live
    state, so the briefing is always in the monitored set.

    Sibling to ``_stamp_connected_messaging_channels``: that one declares
    the integration C-A4 needs; this one declares the delivery the monitor
    needs. A ``session_message`` briefing is C-A4-exempt (self-delivered)
    yet still user-facing for the monitor, so the two stamps are
    independent. No live channel → nothing routable yet → leave as-authored
    (``briefing_activation`` reinstalls when the first channel connects).

    Best-effort: any failure leaves the manifest as-authored. Returns the
    stamped action ids (empty when nothing applied)."""
    try:
        from ..channels import enabled_messaging_channels
        from .native_write import find_existing_spec

        channel = _preferred_live_channel(set(enabled_messaging_channels(bot_id)))
        if not channel:
            return []

        # The bound Spec is the authoritative "is this action user-facing"
        # signal — the extracted manifest entry may have lost it. Best-
        # effort: a missing Spec just means we fall back to whatever the
        # manifest entry itself declares.
        spec_actions: dict[str, dict] = {}
        spec_id = getattr(manifest, "pkg_id", "") or ""
        if spec_id:
            found = find_existing_spec(shared_dir, spec_id)
            if found:
                try:
                    spec = json.loads(found[1].read_text())
                    spec_actions = {
                        a["id"]: a
                        for a in (spec.get("scheduled_actions") or [])
                        if isinstance(a, dict) and a.get("id")
                    }
                except (OSError, json.JSONDecodeError):
                    # Unreadable/garbled Spec → fall back to whatever the
                    # manifest entry itself declares (spec_actions stays empty).
                    spec_actions = {}

        stamped: list[str] = []
        for action in manifest.scheduled_actions or []:
            if not isinstance(action, dict) or not action.get("id"):
                continue
            if (action.get("state") or "active") != "active":
                continue
            spec_action = spec_actions.get(action["id"])
            if not (
                _declares_user_facing_delivery(action)
                or _declares_user_facing_delivery(spec_action)
            ):
                continue
            if _ensure_monitored_delivery(action, spec_action, channel):
                stamped.append(action["id"])

        if stamped:
            save_manifest(manifest, shared_dir)
        return stamped
    except Exception:  # noqa: BLE001
        log.exception(
            "scheduled-delivery contract stamp skipped for %s on %s",
            getattr(manifest, "id", "?"), bot_id,
        )
        return []


def stamp_bot_user_facing_deliveries(
    bot_id: str, shared_dir: Path,
) -> dict[str, list[str]]:
    """Pod-wide, non-forge analogue of the ``approve_forge_job`` delivery
    stamp: walk every active manifest for ``bot_id`` and stamp delivery
    contracts on its user-facing extracted ``scheduled_actions[]`` so they
    enter the proactive-delivery monitor's set.

    Closes the scanner-adoption gap. An app adopted via scanner extraction
    (``quality:"extracted"``, never forge-approved) keeps ``outputs:[]`` +
    ``delivery_contract:null`` even when its bound Spec declares a
    user-facing delivery — so a real recurring delivery can rot unmonitored
    for weeks while every card stays green (the atlas-digest / ledger U1
    class). ``approve_forge_job`` already applies this stamp on the forge
    path; this is the same stamp for the adoption path, invoked from the
    scanner's pod-wide repair leg (forward fix on every scan) and the
    ``application stamp-deliveries`` CLI (immediate backfill).

    Delegates per manifest to :func:`_stamp_scheduled_delivery_contracts`,
    so it is **Spec-gated and live-channel-gated**: an internal action whose
    bound Spec agrees it is not user-facing is left untouched (no false
    ``app_delivery_missed``), and a bot with no connected messaging channel
    is a no-op. Idempotent — a second run on already-stamped manifests
    changes nothing. Returns ``{app_id: [stamped action ids]}`` for the
    manifests that changed (empty dict when nothing applied)."""
    from .manifest import list_manifests

    changed: dict[str, list[str]] = {}
    for manifest in list_manifests(shared_dir, bot_id):
        stamped = _stamp_scheduled_delivery_contracts(manifest, bot_id, shared_dir)
        if stamped:
            changed[getattr(manifest, "id", "?")] = stamped
    return changed


def approve_forge_job(
    job_id: str,
    shared_dir: Path,
    approved_by: str,
    notes: str = "",
    coherence_override_key: str | None = None,
) -> None:
    """
    Called after operator approves a forge job (via admin server or bot conversation).

    Runs Phase 5:
        1. Loads job and manifest
        2. Calls approve_job(job, shared_dir, approved_by, notes) — sets status → approved
        3. Computes new pkg_version (major if exported_hooks changed, else minor)
        4. Calls _apply_forge_output — finalises manifest
        5. Updates improvement_history entry with version + approval metadata
        6. Calls complete_job — moves job to completed dir, saves manifest
    """
    job = load_job(job_id, shared_dir)
    if job is None:
        raise ValueError(f"forge_engine: job {job_id!r} not found for approval")

    if job.status not in ("awaiting_approval",):
        raise ValueError(
            f"forge_engine: job {job_id!r} is in state {job.status!r}, cannot approve"
        )

    _append_log(job_id, shared_dir,
                f"approve_forge_job: approved by {approved_by!r}")

    # Resolve API key and model for Phase 5 spec reconciliation
    api_key       = _resolve_api_key(job.bot_id)
    builder_model, _ = _get_models(shared_dir, bot_id=job.bot_id)

    # Step 9 is the "AWAIT" hold step — mark it done
    mark_step_running(job, 9, shared_dir)
    mark_step_done(job, 9, f"Approved by {approved_by}", shared_dir)

    # Step 10: apply
    mark_step_running(job, 10, shared_dir)

    manifest = load_manifest(job.app_id, job.bot_id, shared_dir)
    if manifest is None:
        err = f"Cannot approve: manifest {job.app_id!r} not found for bot {job.bot_id!r}"
        _append_log(job_id, shared_dir, err)
        mark_step_failed(job, 10, err, shared_dir)
        raise RuntimeError(err)

    # Forge-time test gate removed 2026-06-08 — app-test surface killed
    # per docs/decision-app-tests-2026-06-08.md.

    # ── Connected-channel declaration (U1 activation fix, 2026-06-11) ───────
    stamped = _stamp_connected_messaging_channels(manifest, job.bot_id, shared_dir)
    if stamped:
        _append_log(
            job_id, shared_dir,
            f"Step 10: declared connected messaging channel(s) "
            f"{', '.join(stamped)} in requirements.integrations "
            f"(the bot's live channel state)",
        )

    # ── Delivery-contract declaration for the proactive-delivery monitor ───
    # The realized manifest's scheduled_actions[] are extracted from the
    # workspace and can lose the Spec's outputs[]/delivery_contract, so a
    # user-facing briefing lands invisible to the monitor (ledger U1
    # re-proof, 2026-06-12). Re-assert the delivery intent from live channel
    # state so user-facing scheduled deliveries are always monitored.
    delivery_stamped = _stamp_scheduled_delivery_contracts(
        manifest, job.bot_id, shared_dir
    )
    if delivery_stamped:
        _append_log(
            job_id, shared_dir,
            f"Step 10: declared delivery contract(s) for user-facing "
            f"scheduled action(s) {', '.join(delivery_stamped)} "
            f"(monitored by the proactive-delivery monitor) from live "
            f"channel state",
        )

    # ── Forge-time coherence gate (spec §6.6) ────────────────────────────────
    # Pass A is a BLOCKER for forge approval — manifests with critical or
    # major findings can't ship without an explicit operator override that
    # references the current override_key. The key changes whenever the
    # finding set changes, so a stale override won't smuggle a new
    # incoherence through.
    try:
        from dataclasses import asdict as _asdict
        manifest_dict = _asdict(manifest) if hasattr(manifest, "__dataclass_fields__") else (
            manifest.to_dict() if hasattr(manifest, "to_dict") else dict(manifest)
        )
        # ── Pass C3 LLM dispatch (spec §6.5) ─────────────────────────────
        # When the manifest is structurally coherent enough to be worth
        # judging (Pass A = ok or warnings) and no recent C3 verdict is
        # cached, fire C3 now so the gate immediately below has the
        # capability check to read. Best-effort: failures log + proceed
        # (the gate already had Pass A to work with before this PR).
        _dispatch_c3_for_approval(
            job_id=job_id, shared_dir=shared_dir,
            bot_id=job.bot_id, app_id=job.app_id,
            manifest_dict=manifest_dict,
        )
        validate_coherence_gate(
            manifest_dict,
            bot_id=job.bot_id, app_id=job.app_id,
            override_key=coherence_override_key,
        )
    except ForgeCoherenceGateError as exc:
        _append_log(
            job_id, shared_dir,
            f"Step 10: coherence gate refused approval: {exc} "
            f"(override_key={exc.override_key})",
        )
        mark_step_failed(job, 10, str(exc), shared_dir)
        raise

    # Determine if this is a major bump (exported_hooks changed)
    # We compare the manifest's current exported_hooks against any snapshot
    # stored in context_snapshot (set by the bot session after Phase 1/2 if hooks changed).
    hooks_changed = bool(job.context_snapshot.get("exported_hooks_changed", False))
    new_version = next_pkg_version(manifest.pkg_version or None, major_bump=hooks_changed)

    # Route outcome back to calibration + set job.status → approved
    try:
        approve_job(job, shared_dir, approved_by, notes)
    except Exception as exc:
        err = f"approve_job failed: {exc}"
        _append_log(job_id, shared_dir, err)
        mark_step_failed(job, 10, err, shared_dir)
        raise

    # Apply: finalise manifest state
    try:
        _apply_forge_output(job, manifest, shared_dir, new_version,
                            api_key=api_key, builder_model=builder_model)
    except Exception as exc:
        err = f"_apply_forge_output failed: {exc}"
        _append_log(job_id, shared_dir, err)
        mark_step_failed(job, 10, err, shared_dir)
        raise

    # Store the computed version on the job so improvement_history entry is complete
    job.pkg_version_before = job.pkg_version_before  # already set at job creation
    # complete_job will call build_improvement_history_entry; patch pkg_version_after
    # by temporarily storing on job for the entry builder (it reads job.status == "complete")

    # Reload manifest (save_manifest may have reloaded it from disk)
    manifest = load_manifest(job.app_id, job.bot_id, shared_dir)
    if manifest is None:
        # Shouldn't happen — we just saved it — but be defensive
        from .manifest import ApplicationManifest as _CM
        manifest = _CM(id=job.app_id, name=job.app_id, bot_id=job.bot_id)

    # Patch the most recent improvement_history entry with version + approval metadata.
    # complete_job appends the entry; we pre-populate what it can't know.
    # We'll update AFTER complete_job appends.
    _step10_detail = f"Applied: pkg_version → {new_version}"
    if job.completed_with_errors:
        _n_sched_failed = sum(
            1 for e in (job.context_snapshot.get("scheduled_actions_installed") or [])
            if isinstance(e, dict) and e.get("status") == "failed"
        )
        _step10_detail += (
            f" — {_n_sched_failed} scheduled action install(s) FAILED "
            f"(app shipped; those schedules are not live)"
        )
    mark_step_done(job, 10, _step10_detail, shared_dir)

    try:
        complete_job(job, shared_dir, manifest)
    except Exception as exc:
        err = f"complete_job failed: {exc}"
        _append_log(job_id, shared_dir, err)
        raise

    # Patch the history entry that complete_job just appended
    try:
        manifest = load_manifest(job.app_id, job.bot_id, shared_dir)
        if manifest and manifest.improvement_history:
            last = manifest.improvement_history[-1]
            if last.get("job_id") == job_id:
                last["pkg_version_after"] = new_version
                last["approved_by"]       = approved_by
                last["notes"]             = notes
                save_manifest(manifest, shared_dir)
    except Exception as exc:
        _append_log(job_id, shared_dir,
                    f"Could not patch improvement_history entry: {exc}")

    # ── Native v7-arc cutover (manifest-v7 Slice 3a) ─────────────────────────
    # A completed fresh install is the moment the app becomes real: split
    # the legacy-shaped manifest into Spec + v7-arc Instance + Provenance.
    # MUST run after the improvement_history patch above — that's the last
    # legacy-shaped write in this flow; converting earlier would have the
    # patch's load→save round-trip clobber the Instance with a hydrated
    # legacy dict. Improvement jobs keep their app's existing shape
    # (legacy apps migrate via migrate_v7, not here). Best-effort but
    # loud: a failed conversion leaves the legacy manifest in place,
    # which is valid by construction (consumers branch on manifest_shape).
    if job.job_type == "install":
        try:
            from .native_write import convert_completed_install_to_v7_arc
            # None = nothing to convert (manifest missing, or already
            # v7-arc — e.g. re-install of a migrated app).
            mint = convert_completed_install_to_v7_arc(job, shared_dir)
            if mint is not None and mint.succeeded:
                _append_log(
                    job_id, shared_dir,
                    f"v7-arc native write: spec {mint.spec_id} bound at "
                    f"{mint.spec_path}, instance {mint.instance_id} at "
                    f"{mint.instance_path}"
                    + (f" ({len(mint.warnings)} warning(s))"
                       if mint.warnings else ""),
                )
                for w in mint.warnings:
                    _append_log(job_id, shared_dir, f"v7-arc native write: {w}")
            elif mint is not None:
                _append_log(
                    job_id, shared_dir,
                    "v7-arc native write failed (app stays legacy-shaped, "
                    f"still valid; next migrate_v7 run converts it): "
                    f"{'; '.join(mint.errors)}",
                )
        except Exception as exc:
            _append_log(
                job_id, shared_dir,
                "v7-arc native write failed (app stays legacy-shaped, "
                f"still valid; next migrate_v7 run converts it): {exc}",
            )

    # Post-completion: reconcile actual install cost against the
    # projection captured at create_install_job time, and emit a Signal
    # if the actual run exceeded the projected_cost_high band.
    # Best-effort — never raise; the reconciler logs its own failures.
    try:
        _reconcile_install_cost(job, shared_dir)
    except Exception as exc:
        _append_log(job_id, shared_dir,
                    f"Install-cost reconciliation failed (non-fatal): {exc}")

    _append_log(job_id, shared_dir,
                f"approve_forge_job complete: {job.app_id} → {new_version}")


# ── Post-completion cost reconciliation (2026-06-03) ──────────────────────────
#
# Pairs with install_cost_estimator + the operator-confirmation flow:
# at create_install_job time the projection (mid + high) is captured on
# the ForgeJob. Here, after the install completes, we sum the actual
# turn cost across the bot's forge dispatches in this job's time window
# and (a) write it back to job.actual_cost_usd so the Forge Jobs UI
# can render projected-vs-actual; (b) emit a forge_install_cost_overrun
# Signal when actual > projected_high so the projection model can be
# tuned over time.


def _reconcile_install_cost(job, shared_dir: Path) -> None:
    """Sum actual install cost for a completed forge install and emit an
    overrun Signal if the operator-confirmed projection was wrong.

    ``job`` is the just-completed ForgeJob. The function:
      1. Loads the bot's forge_session windows for the job's date(s)
      2. Filters to the windows belonging to *this* job_id
      3. Loads turns for the bot in that date range, sums cost of any
         turn whose ts lands in a matching window
      4. Updates job.actual_cost_usd via save_job (re-write to completed
         dir; load_job tolerates the rewrite seamlessly)
      5. If job.operator_confirmed AND projection was captured AND
         actual_cost_usd > job.projected_cost_high_usd → emit a
         forge_install_cost_overrun Signal

    Best-effort: failures log and return; the install is already done,
    the reconciliation surface is informational, not load-bearing.
    """
    from datetime import date, datetime, timedelta
    job_id = job.job_id
    # Lazy-import the analyzer modules (evolve-analyzer is an installed package).
    try:
        import forge_sessions as _fs  # type: ignore[import]
        from usage_analytics import load_turns as _load_turns  # type: ignore[import]
        from signals import store as _signals_store  # type: ignore[import]
    except Exception as exc:
        _append_log(job_id, shared_dir,
                    f"reconciler: analyzer-side import failed: {exc}")
        return

    # Job lifecycle dates (UTC) — created_at is set at job-create time;
    # last_updated is freshly bumped by complete_job. The forge_sessions
    # annotation file is partitioned by UTC date so we load all dates
    # touched between create and complete.
    try:
        created_dt = datetime.fromisoformat((job.created_at or "").replace("Z", "+00:00"))
        completed_dt = datetime.fromisoformat((job.last_updated or "").replace("Z", "+00:00"))
    except Exception:
        _append_log(job_id, shared_dir,
                    "reconciler: job created_at/last_updated unparseable; skipping")
        return

    # Span dates between created and completed (inclusive) so a job that
    # crosses UTC midnight still reconciles cleanly.
    dates: list[date] = []
    cur = created_dt.date()
    end = completed_dt.date()
    while cur <= end:
        dates.append(cur)
        cur = cur + timedelta(days=1)

    # Load all forge windows for this bot across the date span; filter to
    # the ones belonging to THIS job_id (the dispatcher writes one per
    # build/critique/refine call, all carrying the same job_id).
    job_windows = []
    for d in dates:
        try:
            for w in _fs.load_windows(shared_dir, job.bot_id, d, include_prev_day=False):
                if w.job_id == job_id:
                    job_windows.append(w)
        except Exception:
            continue

    if not job_windows:
        _append_log(job_id, shared_dir,
                    "reconciler: no forge_session windows for this job — "
                    "actual cost cannot be summed")
        return

    # Sum cost of turns that fall in any of this job's windows.
    actual_cost = 0.0
    try:
        turns = _load_turns(
            job.bot_id, days=max(1, len(dates) + 1), end_date=completed_dt,
            network_path=str(Path(shared_dir) / ".." / "network.json"),
        )
    except Exception as exc:
        _append_log(job_id, shared_dir,
                    f"reconciler: load_turns failed: {exc}")
        return

    for t in turns or []:
        ts_iso = t.get("ts")
        ts_dt = _fs._parse_iso(ts_iso) if ts_iso else None
        if ts_dt is None:
            continue
        for w in job_windows:
            if w.start <= ts_dt <= w.end:
                cost = float(t.get("cost") or 0.0)
                actual_cost += cost
                break

    actual_cost = round(actual_cost, 4)

    # Persist actual_cost back onto the (now-completed) job. load_job
    # finds the file in either active or completed dir; save_job writes
    # to active. To avoid resurrecting the job, write directly to the
    # completed-dir path via the same atomic write used by the lifecycle.
    try:
        from . import forge_jobs as _fj  # type: ignore[import]
        job.actual_cost_usd = actual_cost
        completed_path = _fj._completed_path(job_id, shared_dir)
        if completed_path.exists():
            _fj._atomic_write(completed_path, _fj._job_to_dict(job))
        else:
            # Job didn't reach completed/ — fall back to save_job (active dir)
            _fj.save_job(job, shared_dir)
    except Exception as exc:
        _append_log(job_id, shared_dir,
                    f"reconciler: actual_cost persist failed: {exc}")

    _append_log(
        job_id, shared_dir,
        f"reconciler: actual=${actual_cost:.4f} "
        f"projected_mid=${job.projected_cost_mid_usd or 0:.4f} "
        f"projected_high=${job.projected_cost_high_usd or 0:.4f}",
    )

    # Overrun Signal: only fires for operator-confirmed installs whose
    # projection was captured AND actual exceeds the high-band ceiling
    # (2× mid). Feeds back into projection-model tuning.
    if not getattr(job, "operator_confirmed", False):
        return
    projected_high = getattr(job, "projected_cost_high_usd", None)
    if projected_high is None or projected_high <= 0:
        return
    if actual_cost <= projected_high:
        return

    ratio = (actual_cost / job.projected_cost_mid_usd) if job.projected_cost_mid_usd else None
    try:
        _signals_store.observe(
            shared_dir,
            signature=f"forge/install_overrun/{job.bot_id}/{job.app_id}",
            producer="forge_engine",
            type="forge_install_cost_overrun",
            flavor="maintenance",
            severity="warn",
            scope="bot",
            bot_id=job.bot_id,
            title=(
                f"{job.bot_id}/{job.app_id}: forge install cost "
                f"${actual_cost:.2f} exceeded projected high "
                f"${projected_high:.2f}"
            ),
            body=(
                f"An operator-confirmed install of {job.app_id} on "
                f"{job.bot_id} completed at ${actual_cost:.2f}, above "
                f"the projected high-band ceiling of ${projected_high:.2f}. "
                "The pre-install projection model is under-estimating "
                "this kind of install — surface for retrospective tuning "
                "(see packages/analyzer/install_cost_estimator.py)."
            ),
            details={
                "bot_id": job.bot_id,
                "app_id": job.app_id,
                "job_id": job_id,
                "projected_mid_usd": job.projected_cost_mid_usd,
                "projected_high_usd": projected_high,
                "actual_usd": actual_cost,
                "overrun_ratio": (round(ratio, 3) if ratio is not None else None),
            },
        )
    except Exception as exc:
        _append_log(job_id, shared_dir,
                    f"reconciler: overrun Signal observe failed: {exc}")


# ── Recovery on admin-ui restart ──────────────────────────────────────────────

def recover_orphaned_jobs(shared_dir: Path) -> int:
    """Resume any forge jobs whose daemon thread was killed by an admin-ui
    restart.

    Forge dispatches run in Python daemon threads inside the admin-ui process.
    When launchd auto-respawns admin-ui (KeepAlive=true), those threads die
    silently — the job state on disk reads "running" but no thread is
    advancing it. The bot may have already produced an outbox; we just never
    consumed it.

    On startup, we walk all active jobs in ``status == "running"`` and
    re-invoke ``run_forge_job`` for each in a fresh daemon thread. The
    underlying dispatch primitives are idempotent (resume from existing
    outbox), so jobs whose bot work is already done advance instantly through
    each phase that has a fresh outbox on disk.

    Returns the number of jobs resumed.

    Called from ``web.server.create_app`` at startup. Idempotent — safe to
    call multiple times.
    """
    try:
        from .forge_jobs import list_active_jobs
    except Exception as exc:
        log.warning("recover_orphaned_jobs: could not import list_active_jobs: %s", exc)
        return 0

    try:
        jobs = list_active_jobs(shared_dir)
    except Exception as exc:
        log.warning("recover_orphaned_jobs: list_active_jobs failed: %s", exc)
        return 0

    resumed = 0
    for job in jobs:
        if job.status != "running":
            continue

        # Best-effort log marker for the resumed job — handy for debugging.
        try:
            _append_log(
                job.job_id, shared_dir,
                "Resume: admin-ui restart orphaned the prior daemon thread; "
                "re-invoking run_forge_job. Dispatches are idempotent — "
                "phases with existing outboxes will skip immediately.",
            )
        except Exception:
            pass

        import threading

        def _runner(job_id=job.job_id, bot_id=job.bot_id):
            try:
                run_forge_job(job_id=job_id, shared_dir=shared_dir, bot_id=bot_id)
            except Exception as exc:
                log.warning(
                    "recover_orphaned_jobs: %s resume failed: %s", job_id, exc
                )

        t = threading.Thread(
            target=_runner, daemon=True, name=f"forge-resume-{job.job_id}"
        )
        t.start()
        resumed += 1

    return resumed
