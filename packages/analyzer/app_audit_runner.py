#!/usr/bin/env python3
"""
app_audit_runner.py — Bot-side audit orchestrator (Tier 2 + Tier 3).

Runs from the bot's own LaunchDaemon as the bot user. Three modes:

  --tier 2 (every 6 hours via LaunchDaemon)
    Reads every manifest, runs structural assertions, writes findings to
    the audit outbox + trail.jsonl, stamps ``manifest.last_structural_verify``.
    Pure Python; no LLM. Cheap; always on.

  --tier 3 (hourly tick via LaunchDaemon)
    For every app whose ``audit_eligible=true`` AND cadence-due, runs
    Stage 3a Discovery + Stage 3b Triage via the bot's local OpenClaw
    agent. Writes per-finding outbox records, stamps ``manifest.last_audit``,
    appends to trail.jsonl.

  --pickup-inbox [--request-id X] (one-off, fired by admin kick)
    Reads ``audit_inbox/*.json`` for queued audit requests (from CLI / UI /
    evo handler) and runs them immediately, skipping cadence checks.

All modes serialize via the same lockfile — at most one audit run per bot
at any moment.

Spec: internal/spec-app-audit-2026-05-16.md.

Invocations (from the LaunchDaemon plists / kick command):
    python3 app_audit_runner.py --bot-id <bot> --tier 2
    python3 app_audit_runner.py --bot-id <bot> --tier 3
    python3 app_audit_runner.py --bot-id <bot> --pickup-inbox --request-id <id>
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import getpass
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from evolve_util import atomic_write_json as _atomic_write_json, now_iso as _iso_now

# The plist sets PYTHONPATH={ANALYZER_DIR}, so these work:
from app_audit_structural import (  # noqa: E402
    Finding,
    is_observational_finding,
    run_all as run_structural_assertions,
)
from app_audit_tier3 import (  # noqa: E402
    AuditOutput,
    OUTCOME_AUTO_FIX,
    OUTCOME_DISMISS,
    OUTCOME_PROPOSE,
    Observation,
    TriageDecision,
    run_tier3_for_app,
)
from app_audit_executor import (  # noqa: E402
    ConflictReport,
    ExecutorOutcome,
    execute_auto_fix,
)


logger = logging.getLogger(__name__)

RUNNER_VERSION = "1.4.0"


# ── Coherence pass dispatch (PR coherence-wireup-2026-06-06) ───────────────
#
# Lazily import the admin-side coherence passes. Loaded the first time
# ``_run_coherence_passes`` fires, so this module's other entry points
# stay decoupled from the admin tree (and import failures degrade
# gracefully into "passes skipped" rather than crashing the whole run).
_COHERENCE_LOADED: bool | None = None
_apply_all_pure_passes = None


def _load_coherence_module() -> bool:
    """Import apply_all_pure_passes once.

    Returns True if the module is reachable. Subsequent calls are
    cache hits.
    """
    global _COHERENCE_LOADED, _apply_all_pure_passes
    if _COHERENCE_LOADED is not None:
        return _COHERENCE_LOADED
    try:
        from evolve_admin.applications.pass_runner import (  # type: ignore
            apply_all_pure_passes as _aap,
        )
        _apply_all_pure_passes = _aap
        _COHERENCE_LOADED = True
    except Exception as e:    # noqa: BLE001 — gracefully degrade
        logging.warning(
            "coherence passes unavailable (%s); Tier 2 will skip "
            "coherence/orphan checks until the admin tree is reachable",
            e,
        )
        _COHERENCE_LOADED = False
    return _COHERENCE_LOADED


def _run_coherence_passes(
    *, manifest: dict, workspace: Path, shared_dir: Path,
) -> dict | None:
    """Run Pass A + Pass C1 + orphan check against one manifest.

    Mutates manifest.coherence + manifest.reconciliation in place;
    caller's _stamp_manifest writes the updated dict back to disk.

    Returns the summary dict, or None when the coherence module isn't
    reachable. Never raises — failure here must not abort a Tier 2
    run on real apps.
    """
    if not _load_coherence_module():
        return None
    try:
        return _apply_all_pure_passes(
            manifest,
            workspace_root=workspace,
            shared_dir=shared_dir,
        )
    except Exception as e:    # noqa: BLE001
        logging.warning(
            "coherence pass run failed for manifest id=%r: %s",
            manifest.get("id"), e,
        )
        return None


# ── Audit kinds ─────────────────────────────────────────────────────────────
#
# Workstream B-skills extends the runner from app-only audits to two new
# element types. The "kind" arg routes to the right audit module after
# the lockfile is acquired; inbox handling + lockfile pattern is shared.

KIND_APP = "app"
KIND_SKILL = "skill"
KIND_PROVIDER = "provider"
VALID_KINDS = (KIND_APP, KIND_SKILL, KIND_PROVIDER)


# ── Path resolution ─────────────────────────────────────────────────────────
#
# All paths derive from the bot's home dir, which the runner determines from
# the executing UID rather than the --bot-id arg (the arg names the logical
# bot; the macOS user may differ). This matches the per-bot inference
# architecture: each bot owns its own state.


def _bot_workspace() -> Path:
    """Return the bot's workspace path.

    Reads ``$HOME/.openclaw/openclaw.json`` for the configured workspace if
    present, falling back to ``$HOME/.openclaw/workspace`` (the OC default).
    """
    home = Path.home()
    oc_json = home / ".openclaw" / "openclaw.json"
    try:
        cfg = json.loads(oc_json.read_text())
        ws = cfg.get("agents", {}).get("defaults", {}).get("workspace")
        if ws:
            return Path(ws)
    except (OSError, json.JSONDecodeError):
        pass
    return home / ".openclaw" / "workspace"


def _evolve_dir(workspace: Path) -> Path:
    return workspace / "evolve"


def _manifests_dir(workspace: Path) -> Path:
    return workspace / "manifests"


def _audit_outbox_dir(workspace: Path) -> Path:
    return _evolve_dir(workspace) / "audit_outbox"


def _audit_inbox_dir(workspace: Path) -> Path:
    return _evolve_dir(workspace) / "audit_inbox"


def _audit_inbox_ingested(workspace: Path) -> Path:
    return _audit_inbox_dir(workspace) / "_ingested"


def _audits_dir(workspace: Path) -> Path:
    return _evolve_dir(workspace) / "audits"


def _skill_audits_dir(workspace: Path) -> Path:
    """Per-bot skill-audit trail root — one subdir per skill, parallel to apps."""
    return _evolve_dir(workspace) / "skill_audits"


def _provider_audits_dir(workspace: Path) -> Path:
    """Per-bot provider-audit trail root — one subdir per provider."""
    return _evolve_dir(workspace) / "provider_audits"


def _lockfile_path(workspace: Path) -> Path:
    return _evolve_dir(workspace) / ".audit_runner.lock"


def _pod_config_path(workspace: Path) -> Path:
    return _evolve_dir(workspace) / "pod_config.json"


def _investigations_dir(workspace: Path) -> Path:
    """Per-bot ``evo fail`` investigation trail + per-run JSON root.

    Workstream C — spec-audit-extensions §5.6 deliverable 6. One trail
    file at ``<root>/trail.jsonl`` plus one ``<investigation_id>.json``
    per run. Distinct from ``audits/`` because investigations carry a
    different structured-record shape and a shorter retention (30 days
    per spec §8 Q2).
    """
    return _evolve_dir(workspace) / "investigations"


# ── Pod config (synced from admin) ──────────────────────────────────────────


_DEFAULT_POD_CONFIG = {
    "schema_version": 1,
    "audit": {
        "cadence": "monthly",
        "calibration_mode": True,
        "audit_on_critical_structural": True,
        "tier3_tier": "tier2",
        "ceilings": {
            "max_auto_fix_per_run": 3,
            "max_proposals_per_run": 5,
            "max_tokens_per_audit": 100_000,
        },
    },
    # Workstream B-skills (spec-audit-extensions §4.1): per-bot skill audit.
    # Cadence defaults to weekly — skills are higher-criticality than apps
    # (any app calling a broken skill fails). Calibration mode on for v1.
    "skill_audit": {
        "default_cadence": "weekly",
        "calibration_mode": True,
        "ceilings": {
            "max_auto_fix_per_run": 3,
            "max_proposals_per_run": 5,
            "max_tokens_per_audit": 100_000,
        },
    },
    # Workstream B-skills (spec-audit-extensions §4.2): OAuth-provider audit.
    # Cadence defaults to weekly — credentials are the most failure-prone
    # layer (token rotation, scope changes, OAuth-app revocation).
    "provider_audit": {
        "default_cadence": "weekly",
        "calibration_mode": True,
        "ceilings": {
            "max_auto_fix_per_run": 3,
            "max_proposals_per_run": 5,
            "max_tokens_per_audit": 100_000,
        },
    },
}


def _load_pod_config(workspace: Path) -> dict:
    """Read the synced pod_config.json; fall back to defaults if missing.

    The admin server writes this on every network.json save (see
    audit_pod_config.sync_all_pods). When a freshly-deployed bot runs its
    first audit before the sync hook has fired, falling back to defaults
    keeps the audit working — operators can recover by saving network.json
    once after deploy.
    """
    p = _pod_config_path(workspace)
    if not p.exists():
        return dict(_DEFAULT_POD_CONFIG)
    try:
        data = json.loads(p.read_text())
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return dict(_DEFAULT_POD_CONFIG)


_CADENCE_DAYS = {"daily": 1, "weekly": 7, "monthly": 30, "quarterly": 90}


def _is_tier3_due(manifest: dict, cadence: str, *, now: datetime) -> tuple[bool, str]:
    """Return (due, reason) for whether this app should Tier-3 audit now.

    Honors per-app override (manifest.audit_cadence) when set; otherwise
    uses the bot-effective cadence passed in. ``never`` short-circuits to
    not-due regardless of last_audit state.
    """
    eff = manifest.get("audit_cadence") or cadence
    if eff == "never":
        return False, "cadence=never"
    if eff not in _CADENCE_DAYS:
        return False, f"unknown cadence: {eff!r}"
    interval = timedelta(days=_CADENCE_DAYS[eff])
    last = manifest.get("last_audit") or {}
    verified_at = (last.get("verified_at") or "").strip()
    if not verified_at:
        return True, "never audited"
    try:
        last_dt = datetime.fromisoformat(verified_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return True, f"unparseable last_audit timestamp: {verified_at!r}"
    if now - last_dt >= interval:
        return True, f"overdue (cadence={eff}, last={verified_at})"
    return False, f"not due (next in {(last_dt + interval - now).days}d)"


def _is_first_audit(manifest: dict) -> bool:
    """True iff this app has never been Tier-3 audited.

    A never-audited app's first Tier-3 audit is part of provisioning cost: a
    structural audit of fresh, to-spec forge output. Same condition
    ``_is_tier3_due`` reads for its "never audited" verdict, but computed
    directly off the manifest so it holds regardless of which dispatch entry
    path (cadence sweep / only_apps / force_due) reached the audit. Once the
    first audit stamps ``last_audit.verified_at``, every later audit returns
    False here and inherits the bot's default model — steady-state audits are
    never downgraded by this. Decision C —
    internal/finding-new-bot-activation-cost-2026-06-12.md.
    """
    verified_at = ((manifest.get("last_audit") or {}).get("verified_at") or "").strip()
    return not verified_at


# Grace window before a just-built app's FIRST Tier-3 audit fires. The forge
# builds an app to spec and the overnight cadence sweep would re-audit that
# fresh, to-spec output the same day — ~18% of a new bot's two-day spend went
# to same-day adversarial re-audits that found "no dominant pattern" (finding
# internal/finding-new-bot-activation-cost-2026-06-12.md, decision D). We defer the
# first audit out of the activation window instead. NOT a skip: the app stays
# never-audited (so still cadence-due) and the gate is strictly time-bounded, so
# the first hourly tick past the boundary runs it. Steady-state monthly cadence
# is untouched — this only moves the FIRST audit.
_FIRST_AUDIT_GRACE_DAYS = 7


def _defer_first_audit(manifest: dict, *, now: datetime) -> tuple[bool, str]:
    """Return (defer, reason) for a never-audited app's FIRST Tier-3 audit.

    Defers iff this is the app's first audit (``_is_first_audit``) AND the app
    is younger than ``_FIRST_AUDIT_GRACE_DAYS``. Otherwise returns
    ``(False, reason)`` and the caller audits now.

    Fail-open is the invariant: anything that prevents proving the app is fresh
    (already audited, missing/unparseable ``created_at``) returns ``False`` so
    the audit runs — a deferral must never become a silent coverage gap. And
    because the gate is purely ``now`` vs ``created_at + grace``, it cannot
    defer forever: wall-clock advances past the boundary and the next hourly
    cadence tick runs the first audit. Decision D —
    internal/finding-new-bot-activation-cost-2026-06-12.md.
    """
    if not _is_first_audit(manifest):
        return False, "not first audit"
    created_raw = (manifest.get("created_at") or "").strip()
    if not created_raw:
        # No creation stamp → can't prove the app is fresh → audit now.
        return False, "no created_at (audit now — fail-open)"
    try:
        created_dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False, f"unparseable created_at: {created_raw!r} (audit now — fail-open)"
    if created_dt.tzinfo is None:
        created_dt = created_dt.replace(tzinfo=timezone.utc)
    grace = timedelta(days=_FIRST_AUDIT_GRACE_DAYS)
    age = now - created_dt
    if age >= grace:
        return False, f"first-audit grace elapsed (age={age.days}d ≥ {_FIRST_AUDIT_GRACE_DAYS}d)"
    days_left = max(0, (created_dt + grace - now).days)
    return True, (
        f"first audit deferred — app age {age.days}d < {_FIRST_AUDIT_GRACE_DAYS}d "
        f"grace ({days_left}d to go); will run at +{_FIRST_AUDIT_GRACE_DAYS}d "
        f"or first real usage"
    )


def _provisioning_budget_skip(
    bot_id: str, shared_dir: Path, *, app_id: str, now: datetime,
) -> tuple[bool, str]:
    """Return (skip, reason) for a first audit under the provisioning budget.

    Consults the shared ``provisioning_budget`` gate (one-time ceiling + daily
    cost breaker). ``skip=True`` means this provisioning audit is paused (the
    standup ceiling was reached, or the daily cost breaker is tripped). A
    best-effort Signal is emitted so the pause is observable; on the bot side
    the Signal write may be denied, in which case the per-app trail entry +
    log (written by the caller) is the durable record.

    **Fail-open**: any failure resolving the budget returns ``(False, ...)`` so
    the audit runs — a backstop read failure must never become a silent
    coverage gap (the same invariant as ``_defer_first_audit``). Decision B —
    internal/finding-new-bot-activation-cost-2026-06-12.md.
    """
    try:
        import provisioning_budget as pb  # type: ignore[import]
    except Exception as exc:
        logging.getLogger(__name__).debug(
            "audit: provisioning_budget unavailable (%s); auditing", exc,
        )
        return False, "budget module unavailable (audit now — fail-open)"
    try:
        decision = pb.evaluate(
            bot_id, shared_dir, kind=f"first audit ({app_id})", now=now,
        )
    except Exception as exc:
        logging.getLogger(__name__).debug(
            "audit: provisioning_budget.evaluate raised (%s); auditing", exc,
        )
        return False, "budget eval failed (audit now — fail-open)"
    if decision.allowed:
        return False, "within provisioning budget"
    if decision.signal_payload is not None:
        try:
            pb.emit_signal(shared_dir, decision.signal_payload)
        except Exception as exc:
            # Bot-side Signal writes may be denied ({shared_dir}/signals is
            # evolve-owned); the per-app trail the caller writes is the
            # durable record, so this is non-fatal.
            logging.getLogger(__name__).debug(
                "audit: provisioning budget signal emit failed: %s", exc,
            )
    return True, decision.reason


def _resolve_first_audit_model(bot_id: str) -> str | None:
    """Resolve the model for a just-built app's FIRST Tier-3 audit.

    Pins the first (provisioning) audit to the pod's ``standard`` role
    instead of letting it inherit the bot's default agent model — which is
    the ``power`` rung for a ``full``-tier bot, the shape that audited 6 new
    apps on Opus on the `ledger` pod. The structural audit of fresh
    starter-pack output doesn't earn the power multiplier; ``standard`` is
    ~4–5× cheaper (decision C —
    internal/finding-new-bot-activation-cost-2026-06-12.md).

    Goes through the role/tier resolver (``resolve_tier`` accepts the
    ``standard`` role ID directly), so a per-bot or pod-wide ``standard``
    override is respected and no provider/model name appears in this logic.
    Returns ``None`` when the role can't be resolved (broken config / test
    isolation), so the caller passes ``model=None`` and the dispatch falls
    back to the bot's agent default — today's behavior. Fail-safe toward
    "no change".
    """
    try:
        from evolve_config import load_config  # type: ignore
        from models import resolve_tier  # type: ignore
        return resolve_tier("standard", load_config(), bot_id=bot_id)
    except Exception as exc:
        logging.getLogger(__name__).debug(
            "audit: standard-role resolve failed, inheriting bot default: %s",
            exc,
        )
        return None


# ── Lockfile ────────────────────────────────────────────────────────────────
#
# fcntl.LOCK_EX | LOCK_NB so a second invocation exits immediately if held.
# Spec: at most one audit run per bot at any time.


class LockBusy(Exception):
    """Another audit run holds the lock; this invocation should exit cleanly."""


def _acquire_lock(workspace: Path):
    """Open the lockfile and grab an exclusive non-blocking flock.

    Returns the open file handle (must stay open for the lock to be held).
    Raises LockBusy if another process holds the lock.
    """
    lock_path = _lockfile_path(workspace)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        fh.close()
        if e.errno in (errno.EAGAIN, errno.EACCES):
            raise LockBusy() from e
        raise
    fh.seek(0)
    fh.truncate()
    fh.write(f"{os.getpid()}\n{_iso_now()}\n")
    fh.flush()
    return fh


# ── Time + ID helpers ───────────────────────────────────────────────────────


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# ── Manifest I/O ────────────────────────────────────────────────────────────


def _load_manifests(workspace: Path) -> list[tuple[Path, dict]]:
    """Return [(path, manifest_dict)] for every manifest on the bot.

    Skips hidden files, _history archives, and files that fail to parse.
    """
    out: list[tuple[Path, dict]] = []
    d = _manifests_dir(workspace)
    if not d.exists():
        return out
    for f in sorted(d.iterdir()):
        if f.suffix != ".json":
            continue
        if f.name.startswith("_") or f.name.startswith("."):
            continue
        try:
            data = json.loads(f.read_text())
            if not isinstance(data, dict):
                continue
            out.append((f, data))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _stamp_manifest(path: Path, manifest: dict, stamp: dict) -> bool:
    """Atomically update manifest.last_structural_verify (Tier-2 path).

    Thin wrapper around ``_stamp_manifest_field`` for back-compat with the
    Tier-2 call sites that pre-date the Tier-3 stamps.
    """
    return _stamp_manifest_field(path, manifest, "last_structural_verify", stamp)


# ── Crontab snapshot ────────────────────────────────────────────────────────


def _crontab_lines() -> list[str]:
    """Snapshot the running user's crontab as a list of non-comment lines.

    Returns an empty list if crontab is unavailable or returns non-zero.
    The assertion that consumes this treats an empty crontab as "no live
    entries to compare against," which is the right behavior for bots
    without any cron-scheduled work.
    """
    try:
        r = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return []
        lines = []
        for raw in r.stdout.splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            lines.append(stripped)
        return lines
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []


def _openclaw_cron_snapshot() -> tuple[list[dict] | None, dict[str, list[dict]]]:
    """Snapshot the bot's openclaw cron list + recent-run history per job.

    Spec: internal/spec-app-coherence-and-reconciliation-2026-06-05.md §17.3 + Q32.

    Returns ``(cron_list, run_history_by_id)``:

    * ``cron_list`` — list of cron-job dicts from ``openclaw cron list --json``,
      or ``None`` when the CLI is unavailable. ``None`` (vs empty list) signals
      to the check_openclaw_cron_run_status assertion that the snapshot itself
      failed, so the assertion silently skips rather than firing false
      positives during transient OC CLI outages.

    * ``run_history_by_id`` — dict mapping each job's UUID to a list of recent
      runs (newest first), from ``openclaw cron runs --json --id <jid>
      --limit 5``. Empty list for a job means no recorded runs (likely just
      installed), which the assertion treats as "no finding."

    The runner runs as the bot user, so ``openclaw`` invocations resolve
    without bot-id forwarding. We invoke via subprocess rather than via the
    admin-side oc_cli helpers because those helpers expect to dispatch from
    a different user via sudo — overkill when the runner is already the bot.
    """
    cron_list: list[dict] | None = None
    try:
        r = subprocess.run(
            ["openclaw", "cron", "list", "--json"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            cron_list = json.loads(r.stdout or "[]")
            if not isinstance(cron_list, list):
                cron_list = []
    except (FileNotFoundError, subprocess.TimeoutExpired,
            json.JSONDecodeError, OSError):
        cron_list = None  # signals "CLI unavailable" to the assertion

    run_history_by_id: dict[str, list[dict]] = {}
    if cron_list:
        for job in cron_list:
            if not isinstance(job, dict):
                continue
            jid = job.get("id") or job.get("job_id") or ""
            if not jid:
                continue
            try:
                rr = subprocess.run(
                    ["openclaw", "cron", "runs", "--json",
                     "--id", jid, "--limit", "5"],
                    capture_output=True, text=True, timeout=10,
                )
                if rr.returncode != 0:
                    run_history_by_id[jid] = []
                    continue
                payload = json.loads(rr.stdout or "{}")
                # openclaw cron runs --json returns {"entries": [...]} per
                # the schema used by the existing oc_cli wrapper; tolerate
                # bare list as a fallback for older builds.
                entries: list = []
                if isinstance(payload, dict):
                    entries = payload.get("entries") or []
                elif isinstance(payload, list):
                    entries = payload
                run_history_by_id[jid] = [e for e in entries if isinstance(e, dict)]
            except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
                # Per-job failure is non-fatal — record empty and move on.
                run_history_by_id[jid] = []

    return cron_list, run_history_by_id


def _launchctl_labels() -> list[str]:
    """Snapshot the loaded service labels for the running user.

    Routes through the process-wide Scheduler seam's ``list()`` verb, so a
    Linux pod resolves the injected SystemdScheduler and returns the REAL
    systemd label set instead of an empty list (improves the app-audit cron
    cross-check; read-only, no destructive verb). Returns an empty list on any
    failure — the cron_labels_loaded assertion handles an empty list as
    "no labels to compare against."

    On macOS the audit runner executes as the bot user, whose own launchd
    domain is exactly the snapshot we want — and bot users hold no launchctl
    sudo grants, so ``get_scheduler()``'s sudo-by-default would break the read.
    We therefore guarded-derive an unsudo'd launchd adapter ONLY when the
    active adapter is launchd (reusing the injected fake's runner under test),
    byte-identical to the pre-seam ``LaunchdScheduler(use_sudo=False,
    timeout=5.0)`` argv. On a non-launchd pod the injected adapter answers
    directly — ``use_sudo`` is a launchd argv concept SystemdScheduler carries
    via its own posture set by the platform gate. Same guarded-derive shape as
    mcp_service._scheduler_nosudo.
    """
    from runtime.scheduler import LaunchdScheduler, get_scheduler

    try:
        sched = get_scheduler()
        if not isinstance(sched, LaunchdScheduler):
            return sched.list()
        if sched._custom_runner:
            return LaunchdScheduler(
                use_sudo=False, runner=sched._runner, timeout=5.0,
            ).list()
        return LaunchdScheduler(use_sudo=False, timeout=5.0).list()
    except Exception:
        return []


# ── v16: openclaw.json hooks + LaunchAgents enumeration for the verifier ────
#
# The runner runs as the bot user (via the LaunchDaemon plist), so direct
# reads succeed. No sudo, no admin roundtrip — same posture as the existing
# crontab / launchctl snapshots above.


def _read_openclaw_hooks_block() -> dict:
    """Read the bot's openclaw.json hooks block.

    Returns the parsed ``hooks`` dict (typically ``{event: [entry, …], …}``),
    or ``{}`` on any failure. The structural assertions consume this so
    A1 can resolve ``openclaw.json#hooks.<event>`` install artifacts and
    A6 can enumerate registered hooks for the orphan check.
    """
    oc_json = Path.home() / ".openclaw" / "openclaw.json"
    try:
        raw = oc_json.read_bytes()
    except (OSError, PermissionError):
        return {}
    try:
        config = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    block = (config or {}).get("hooks")
    return block if isinstance(block, dict) else {}


def _enumerate_launch_agents_for_audit() -> list[dict]:
    """Enumerate ``~/Library/LaunchAgents/*.plist`` for A1 + A6 cross-check.

    Returns a list of dicts mirroring the scanner's enumeration shape:
    ``{label, plist_path, program_args, start_interval,
       start_calendar_interval, run_at_load}``. Per-entry parsing failures
    are silent — the assertion just won't be able to attribute that one
    install. Returns ``[]`` when the LaunchAgents dir doesn't exist.
    """
    import plistlib
    la_dir = Path.home() / "Library" / "LaunchAgents"
    if not la_dir.exists():
        return []
    out: list[dict] = []
    try:
        plist_paths = sorted(la_dir.glob("*.plist"))
    except (OSError, PermissionError):
        return []
    for plist_path in plist_paths:
        try:
            raw = plist_path.read_bytes()
            parsed = plistlib.loads(raw)
        except (OSError, plistlib.InvalidFileException, ValueError, Exception):
            continue
        if not isinstance(parsed, dict):
            continue
        out.append({
            "label": parsed.get("Label", "") or "",
            "plist_path": str(plist_path),
            "program_args": list(parsed.get("ProgramArguments") or []),
            "start_interval": parsed.get("StartInterval"),
            "start_calendar_interval": parsed.get("StartCalendarInterval"),
            "run_at_load": bool(parsed.get("RunAtLoad", False)),
        })
    return out


def _collect_all_installed_artifacts(manifests: list[tuple[Path, dict]]) -> set[str]:
    """Build the union of every ``scheduled_actions[].installed_artifact``
    across all manifests on the bot.

    A6 (orphan install) needs the cross-manifest view to decide what's
    actually unclaimed — a per-manifest check would emit false positives
    on every app for every install belonging to a sibling. Empty values
    are skipped; both the artifact string and the plist label (when
    derivable from ``install.plist_label``) are included so manifests that
    only carry one shape still match.
    """
    out: set[str] = set()
    for _path, manifest in manifests:
        if not isinstance(manifest, dict):
            continue
        for action in manifest.get("scheduled_actions") or []:
            if not isinstance(action, dict):
                continue
            artifact = (action.get("installed_artifact") or "").strip()
            if artifact:
                out.add(artifact)
            install = action.get("install") or {}
            if isinstance(install, dict):
                for key in ("plist_label", "label"):
                    val = (install.get(key) or "").strip()
                    if val:
                        out.add(val)
    return out


# ── Outbox write ────────────────────────────────────────────────────────────


# ── Emit-on-change cursor (footprint cut, 2026-06-28) ─────────────────────────
#
# Spec: internal/footprint-disk-output-audit-2026-06-28.md.
#
# Tier 2 runs every 6 hours and used to write one outbox record PER FINDING
# EVERY run, re-shipping unchanged findings forever. A single stable finding
# minted 4 records/day that all collapsed into ONE Signal on the admin side
# (signature dedup) — ~90% of the records were redundant re-emissions and the
# dominant volume engine behind the audit-store sediment.
#
# The cursor records, per finding signature, the content-hash of the last
# record we emitted. On each run we write an outbox record ONLY when a
# finding's signature is new OR its payload changed since the last emit. The
# downstream Signal already exists for an unchanged finding, so skipping the
# write just forgoes an observation_count bump (an acceptable staleness).
#
# RESOLUTION is unaffected: it is driven entirely by the run-summary's
# ``kept_signatures`` list → ``signals.store.sweep_resolve`` on the admin
# side. The runner keeps listing EVERY current finding's signature in
# ``kept_signatures`` regardless of whether a per-finding record was written,
# so a finding that stops firing drops out of the keep-set and its Signal is
# resolved — emit-suppression never strands a stale firing Signal.
#
# The cursor is self-contained (a single dotfile in the outbox dir) and is
# rebuilt each run to hold ONLY this run's signatures, so cleared findings
# self-prune. It does NOT depend on the presence of any outbox record or the
# ``_ingested`` dir — robust to the companion delete-on-ingest sweep.
_EMITTED_CURSOR_NAME = ".emitted.json"


def _emitted_cursor_path(outbox_dir: Path) -> Path:
    return outbox_dir / _EMITTED_CURSOR_NAME


def _load_emitted_cursor(outbox_dir: Path) -> dict:
    """Load the per-bot emitted-cursor. Returns a fresh skeleton on any
    read/parse failure — a missing or corrupt cursor degrades to
    "everything is new" (re-emits this run, then converges), never a crash.
    """
    path = _emitted_cursor_path(outbox_dir)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return {"version": 1, "signatures": {}}
    if not isinstance(data, dict) or not isinstance(data.get("signatures"), dict):
        return {"version": 1, "signatures": {}}
    return data


def _save_emitted_cursor(outbox_dir: Path, cursor: dict) -> None:
    """Atomically persist the emitted-cursor (temp-file + rename)."""
    outbox_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(_emitted_cursor_path(outbox_dir), cursor)


def _finding_payload_hash(finding: "Finding") -> str:
    """Content-hash of the Signal-relevant payload of a finding.

    Covers exactly the fields that, if changed, should re-ship the record so
    the downstream Signal body/severity updates: assertion_id, severity,
    summary, evidence. Deliberately EXCLUDES per-run churn (record_id, ts,
    audit_run_id, runner_version) — including those would defeat dedup and
    re-emit every finding every run, the very thing the cursor prevents.
    """
    blob = json.dumps(
        {
            "assertion_id": finding.assertion_id,
            "severity": finding.severity,
            "summary": finding.summary,
            "evidence": finding.evidence,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _write_outbox_record(outbox_dir: Path, record: dict) -> Path:
    """Atomic write of one outbox record. Returns the written path."""
    outbox_dir.mkdir(parents=True, exist_ok=True)
    rid = record.get("record_id") or _new_id("rec")
    record["record_id"] = rid
    path = outbox_dir / f"{rid}.json"
    _atomic_write_json(path, record)
    return path


# ── Trail append ────────────────────────────────────────────────────────────
#
# Per-app trail.jsonl is the durable rolling log (§5.4 in spec). The audit
# runner appends summary + finding entries; the admin UI reads via ACL.
#
# Retention (footprint cut, 2026-06-28; internal/footprint-antipattern-cross-app-
# audit-2026-06-28.md). The trail had NO prune: Tier-2 runs every ~6h and
# re-appended every persistent finding forever (one app's trail held 1994 lines,
# ~5 distinct signatures, the top one re-emitted 1065×). We soft-cap it the same
# way investigations/ is capped (_prune_investigations): once a trail exceeds
# 1000 lines it is rewritten down to the most-recent 500. Every reader is
# bounded-tail (the UI trail modal, the evo tray, the Tier-3 LLM, compare-to-N-
# days all take the most-recent slice) so dropping the head is safe. The cap
# runs from inside _append_trail, and the per-run ``audit_run`` summary line is
# written every run, so a trail that has stopped accruing finding lines (Change
# B below) is still capped at least once per run.
_TRAIL_CAP_LINES = 1000
_TRAIL_CAP_KEEP = 500


def _cap_trail(trail: Path) -> None:
    """Soft-cap a trail.jsonl to its most-recent lines. Best-effort; never raises."""
    try:
        lines = trail.read_text().splitlines()
    except OSError:
        return
    if len(lines) > _TRAIL_CAP_LINES:
        try:
            trail.write_text("\n".join(lines[-_TRAIL_CAP_KEEP:]) + "\n")
        except OSError as exc:
            # Best-effort cap: a failed rewrite just leaves the trail oversized
            # until the next append retries — never fail the audit run.
            logger.warning("audit: trail cap rewrite failed for %s: %s", trail, exc)


def _append_trail(audits_dir: Path, app_id: str, entry: dict) -> Path:
    app_audit_dir = audits_dir / app_id
    app_audit_dir.mkdir(parents=True, exist_ok=True)
    trail = app_audit_dir / "trail.jsonl"
    with trail.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")
    _cap_trail(trail)
    return trail


# ── Tier 2 run ──────────────────────────────────────────────────────────────


def run_tier2(
    workspace: Path,
    *,
    bot_id: str,
    shared_dir: Path,
    audit_run_id: str | None = None,
) -> dict:
    """Run Tier-2 structural assertions across every manifest on the bot.

    Returns a summary dict. Side effects:
      - Outbox records written per-finding + one run_summary.
      - Trail entries appended per app.
      - manifest.last_structural_verify stamped on each manifest.

    Idempotent w.r.t. Signal-store dedup: each finding's signature is stable
    so repeated runs of the same failing assertion against the same target
    converge on a single Signal on the admin side.
    """
    run_id = audit_run_id or _new_id("audit-run")
    started_at = _iso_now()
    outbox_dir = _audit_outbox_dir(workspace)
    audits_dir = _audits_dir(workspace)

    manifests = _load_manifests(workspace)

    # ── v16 install-site verification (spec-forge-side-effects §7) ───────────
    # v17: A1/A5 resolve HEARTBEAT.md / AGENTS.md sections directly from
    # workspace (no ctx prep needed — the assertion reads the file). A6
    # walks evolve-managed sections (same workspace reads) plus enumerates
    # ~/Library/LaunchAgents/ via _enumerate_launch_agents_for_audit so the
    # cross-app orphan check has both surfaces. The pre-v17
    # openclaw_hooks_block remains in ctx (always {}) so any in-flight
    # consumer doesn't KeyError; remove in v18.
    bot_launchd_entries = _enumerate_launch_agents_for_audit()
    all_pod_installed_artifacts = _collect_all_installed_artifacts(manifests)

    # v20: openclaw cron snapshot for the openclaw_cron_run_status assertion
    # (spec-app-coherence-and-reconciliation-2026-06-05.md §17.3 + Q32).
    # The load-bearing Pass B check — catches team-bot-a-task-worker, personal-bot-backup,
    # evolve security:cve-scan, security-bot-weekly-self-audit and friends. Snapshot
    # is taken once per Tier-2 run and read across every manifest's assertion
    # invocation.
    openclaw_crons, openclaw_run_history = _openclaw_cron_snapshot()

    ctx = {
        "workspace": workspace,
        "crontab_lines": _crontab_lines(),
        "launchctl_labels": _launchctl_labels(),
        "python_bin": sys.executable or "python3",
        # v17 (deprecated): kept as empty dict so in-flight consumers don't
        # KeyError. The verifier's hook-mechanism branch always reports
        # missing-install anyway, so an empty hooks block is correct.
        "openclaw_hooks_block": {},
        "bot_launchd_entries": bot_launchd_entries,
        "all_pod_installed_artifacts": all_pod_installed_artifacts,
        # v20: cron run-status snapshot. None for openclaw_crons signals
        # "CLI unavailable" — the assertion silently skips rather than firing
        # false positives.
        "openclaw_crons": openclaw_crons,
        "openclaw_run_history_by_id": openclaw_run_history,
        # v24: pod-shared dir for the v7-arc Instance → Spec binding
        # integrity check (orphan_v7_arc_instance). Consumed by
        # check_v7_arc_instance_has_spec_binding to verify the bound
        # Spec file actually exists under {shared_dir}/gallery/.
        "shared_dir": shared_dir,
    }

    all_signatures: list[str] = []
    apps_with_findings = 0
    severity_counts = {"critical": 0, "major": 0, "minor": 0, "info": 0}
    total_findings = 0
    coherence_summary_by_status: dict[str, int] = {}
    orphans_confirmed = 0

    # Emit-on-change cursor (footprint cut). Load the prior run's per-signature
    # content-hashes; build a fresh cursor holding only this run's signatures
    # (so cleared findings self-prune). Counters feed the run-summary so the
    # cut is observable rather than silent.
    prior_emitted = (_load_emitted_cursor(outbox_dir).get("signatures") or {})
    new_emitted: dict[str, dict] = {}
    records_written = 0
    records_suppressed_unchanged = 0
    findings_observational_suppressed = 0
    trail_lines_suppressed_unchanged = 0

    for path, manifest in manifests:
        if manifest.get("status") in ("hidden", "dormant", "deprecated"):
            continue
        app_id = manifest.get("id") or path.stem
        findings = run_structural_assertions(manifest, ctx)

        # Coherence + orphan passes — mutates manifest.coherence and
        # manifest.reconciliation in place. The _stamp_manifest call
        # below writes the full manifest back, including these mutations.
        # Failures here are non-fatal (logged and skipped) so a transient
        # admin-tree problem doesn't break the existing Tier 2 surface.
        coh_summary = _run_coherence_passes(
            manifest=manifest, workspace=workspace, shared_dir=shared_dir,
        )
        if coh_summary is not None:
            status = coh_summary.get("status", "ok")
            coherence_summary_by_status[status] = (
                coherence_summary_by_status.get(status, 0) + 1
            )
            if coh_summary.get("orphan_state") == "orphan":
                orphans_confirmed += 1

        # Per-app summary stamp into the manifest itself
        stamp = {
            "verified_at": started_at,
            "runner_version": RUNNER_VERSION,
            "audit_run_id": run_id,
            "status": _status_for(findings),
            "findings_count": len(findings),
            "by_severity": _by_severity(findings),
        }
        _stamp_manifest(path, manifest, stamp)

        # Trail summary entry — always written, even when zero findings, so
        # operators can see audits ran.
        _append_trail(audits_dir, app_id, {
            "ts": started_at,
            "kind": "audit_run",
            "tier": 2,
            "audit_run_id": run_id,
            "status": stamp["status"],
            "findings_count": len(findings),
            "by_severity": stamp["by_severity"],
        })

        if findings:
            apps_with_findings += 1
            total_findings += len(findings)
            # All structural-verifier findings on the same (bot, app) share
            # the same root cause: "fix this manifest." The admin poller maps
            # this onto Signal.incident_key so the Alerts UI collapses them
            # into one expandable row per manifest instead of 4-6 fragments.
            # Spec: internal/spec-recommendations-rework-2026-06-02.md
            # ("Coalescing rules" / "coalesce_key").
            coalesce_key = f"app_structural:{bot_id}:{app_id}"
            provenance = manifest.get("provenance")
            for f in findings:
                severity_counts[f.severity] = severity_counts.get(f.severity, 0) + 1
                sig = f.signature(bot_id, app_id)
                # ALWAYS list the signature so the run-summary's sweep_resolve
                # keeps this finding's Signal alive. Suppressing the per-finding
                # record below never strands a stale Signal — resolution is
                # driven by the keep-set, not by per-finding records.
                all_signatures.append(sig)

                # Emit-on-change decision, computed once and shared by the trail
                # source-cut (just below) and the outbox write (further down):
                # is this finding new, or did its Signal-relevant payload change
                # since the last run?
                payload_hash = _finding_payload_hash(f)
                prev = prior_emitted.get(sig)
                changed = not isinstance(prev, dict) or prev.get("hash") != payload_hash

                # ── Change C: source-cut the per-finding trail line ──────────
                # The trail entry is the durable local log, but a persistent
                # finding re-fires every ~6h and used to re-append a byte-
                # identical tier2_finding line EVERY run (one signature appeared
                # 1065× in the 2026-06-28 audit). Append only when the finding
                # is new or its payload changed; the per-run ``audit_run``
                # summary line above is still written every run, and every trail
                # reader is bounded-tail, so the latest state is always present.
                if changed:
                    _append_trail(audits_dir, app_id, {
                        "ts": _iso_now(),
                        "kind": "tier2_finding",
                        "audit_run_id": run_id,
                        "assertion_id": f.assertion_id,
                        "severity": f.severity,
                        "summary": f.summary,
                        "signature": sig,
                    })
                else:
                    trail_lines_suppressed_unchanged += 1

                # ── Change B: observational upstream gate ────────────────────
                # When the finding targets a field whose provenance is
                # observational, the admin poller would drop it on arrival
                # (never a Signal). Don't ship it at all — apply the SAME
                # shared predicate here so the bot doesn't write + transmit a
                # record the admin is guaranteed to discard. The trail above
                # still captures it (operators see observational findings in
                # the audit trail). It is also excluded from the cursor so it
                # never blocks a future authored re-emit.
                if is_observational_finding(f.assertion_id, provenance):
                    findings_observational_suppressed += 1
                    continue

                # ── Change A: emit-on-change ─────────────────────────────────
                # Write an outbox record only when this finding is new or its
                # payload changed since the last emit. Carry the prior
                # last_emitted forward for unchanged findings; record last_seen
                # so a future operator tool can age the cursor if needed.
                seen_at = _iso_now()
                if changed:
                    _write_outbox_record(outbox_dir, {
                        "record_id": _new_id("rec"),
                        "audit_run_id": run_id,
                        "kind": "tier2_finding",
                        "ts": seen_at,
                        "runner_version": RUNNER_VERSION,
                        "producer": "app_structural_verifier",
                        "bot_id": bot_id,
                        "app_id": app_id,
                        "signature": sig,
                        "coalesce_key": coalesce_key,
                        "assertion_id": f.assertion_id,
                        "severity": f.severity,
                        "summary": f.summary,
                        "evidence": f.evidence,
                    })
                    records_written += 1
                    last_emitted = seen_at
                else:
                    records_suppressed_unchanged += 1
                    last_emitted = (
                        prev.get("last_emitted") if isinstance(prev, dict) else None
                    ) or seen_at
                new_emitted[sig] = {
                    "hash": payload_hash,
                    "last_emitted": last_emitted,
                    "last_seen": seen_at,
                }

    # Persist the rebuilt emit-on-change cursor (only this run's signatures,
    # so cleared findings drop out). Best-effort: a cursor write failure must
    # not fail the audit run — worst case the next run re-emits unchanged
    # findings once and re-converges.
    try:
        _save_emitted_cursor(outbox_dir, {"version": 1, "signatures": new_emitted})
    except OSError as exc:
        logger.warning(
            "audit: failed to persist emitted-cursor for %s: %s", bot_id, exc,
        )

    if (
        records_suppressed_unchanged
        or findings_observational_suppressed
        or trail_lines_suppressed_unchanged
    ):
        logger.info(
            "audit: tier2 emit cut — %d records written, %d unchanged "
            "suppressed, %d observational suppressed, %d trail lines suppressed "
            "(bot=%s)",
            records_written, records_suppressed_unchanged,
            findings_observational_suppressed, trail_lines_suppressed_unchanged,
            bot_id,
        )

    # Run-summary record. Carries the full kept-signatures list so the
    # admin poller can sweep-resolve everything that didn't fire this run.
    completed_at = _iso_now()
    _write_outbox_record(outbox_dir, {
        "record_id": _new_id("rec"),
        "audit_run_id": run_id,
        "kind": "tier2_run_summary",
        "ts": completed_at,
        "runner_version": RUNNER_VERSION,
        "bot_id": bot_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "apps_audited": len(manifests),
        "apps_with_findings": apps_with_findings,
        "total_findings": total_findings,
        "by_severity": severity_counts,
        "kept_signatures": all_signatures,
        # Emit-on-change accounting (footprint cut) — surfaced so the cut is
        # observable in the trail/admin rather than a silent suppression.
        "records_written": records_written,
        "records_suppressed_unchanged": records_suppressed_unchanged,
        "findings_observational_suppressed": findings_observational_suppressed,
        "trail_lines_suppressed_unchanged": trail_lines_suppressed_unchanged,
        # Coherence-pass results, surfaced for the admin poller. Empty
        # dict when the coherence module wasn't reachable.
        "coherence_by_status": coherence_summary_by_status,
        "orphans_confirmed": orphans_confirmed,
    })

    return {
        "audit_run_id": run_id,
        "apps_audited": len(manifests),
        "apps_with_findings": apps_with_findings,
        "total_findings": total_findings,
        "records_written": records_written,
        "records_suppressed_unchanged": records_suppressed_unchanged,
        "findings_observational_suppressed": findings_observational_suppressed,
        "trail_lines_suppressed_unchanged": trail_lines_suppressed_unchanged,
        "by_severity": severity_counts,
        "coherence_by_status": coherence_summary_by_status,
        "orphans_confirmed": orphans_confirmed,
    }


# ── Tier 3 run ──────────────────────────────────────────────────────────────


def run_tier3(
    workspace: Path,
    *,
    bot_id: str,
    shared_dir: Path,
    audit_run_id: str | None = None,
    only_apps: list[str] | None = None,
    full_audit: bool = False,
    force_due: bool = False,
) -> dict:
    """Run Tier-3 audits across this bot's apps.

    Behavior:
      - When only_apps is None, audit every eligible app whose cadence is due.
      - When only_apps is a list, audit just those apps (skip cadence check).
      - ``force_due=True`` skips the cadence check for the cadence-driven mode
        too — used by the Tier-2 critical → Tier-3 self-hint path.
      - ``full_audit=True`` excludes manifest.audit_accepted[] from Stage 3a's
        input so accepted findings get re-evaluated.

    Side effects:
      - One outbox record per Stage-3b decision (tier3_finding) + one per
        conflict_notice + one tier3_run_summary at the end.
      - Trail entries per audit + per finding.
      - manifest.last_audit stamped on each audited app.
    """
    run_id = audit_run_id or _new_id("tier3-run")
    started_at = _iso_now()
    outbox_dir = _audit_outbox_dir(workspace)
    audits_dir = _audits_dir(workspace)

    pod_config = _load_pod_config(workspace)
    cadence = pod_config.get("audit", {}).get("cadence", "monthly")
    calibration_on = bool(pod_config.get("audit", {}).get("calibration_mode", True))
    ceilings = pod_config.get("audit", {}).get("ceilings", {})
    max_tokens = int(ceilings.get("max_tokens_per_audit", 100_000))
    max_proposals = int(ceilings.get("max_proposals_per_run", 5))
    max_auto_fix = int(ceilings.get("max_auto_fix_per_run", 3))

    manifest_pairs = _load_manifests(workspace)
    # Pre-compute the full set for cross-app conflict checks
    all_manifest_dicts = [m for _, m in manifest_pairs]
    now = datetime.now(timezone.utc)

    apps_audited = 0
    apps_skipped_ineligible = 0
    apps_skipped_not_due = 0
    apps_first_audit_deferred = 0
    total_outcomes = {"dismiss": 0, "auto_fix": 0, "propose": 0, "conflict_notice": 0}
    total_tokens = 0

    for path, manifest in manifest_pairs:
        app_id = manifest.get("id") or path.stem
        if only_apps and app_id not in only_apps:
            continue
        if manifest.get("status") in ("hidden", "dormant", "deprecated"):
            continue
        if not manifest.get("audit_eligible", True) and not only_apps:
            # Cadence-driven runs honor audit_eligible. Explicit per-app
            # requests (only_apps not None) bypass it.
            apps_skipped_ineligible += 1
            continue
        if not only_apps and not force_due:
            due, reason = _is_tier3_due(manifest, cadence, now=now)
            if not due:
                apps_skipped_not_due += 1
                continue
            # Decision D: defer a just-built app's FIRST (provisioning) audit
            # out of the activation window. The cadence sweep would otherwise
            # re-audit fresh, to-spec forge output the same day it was built.
            # This is a deferral, not a skip — the app stays never-audited (so
            # still cadence-due) and the gate is time-bounded, so the first
            # hourly tick past the +7d boundary runs it. Manual (only_apps) and
            # critical-structural escalation (force_due) audits are never
            # deferred — they don't reach this branch.
            defer, defer_reason = _defer_first_audit(manifest, now=now)
            if defer:
                apps_first_audit_deferred += 1
                logger.info(
                    "audit: deferring first Tier-3 audit of %s/%s — %s",
                    bot_id, app_id, defer_reason,
                )
                # Observable in the per-app trail so the deferral (and the
                # boundary it will fire at) shows in the UI, not just the log.
                _append_trail(audits_dir, app_id, {
                    "ts": _iso_now(),
                    "kind": "audit_deferred",
                    "tier": 3,
                    "audit_run_id": run_id,
                    "reason": defer_reason,
                    "created_at": manifest.get("created_at") or "",
                    "grace_days": _FIRST_AUDIT_GRACE_DAYS,
                })
                continue

            # Decision B: a first (provisioning) audit also respects the bot's
            # provisioning budget — the one-time ceiling + the daily cost
            # breaker. Composes with D (which defers by TIME): an audit only
            # reaches here if D let it through, and this refuses by BUDGET. So a
            # first audit won't run while the bot's daily cost breaker is
            # tripped (the audit pipeline used to bleed past the trip), nor once
            # the cumulative standup ceiling is reached. Steady-state (non-first)
            # audits and manual/forced audits don't reach this branch.
            if _is_first_audit(manifest):
                budget_skip, budget_reason = _provisioning_budget_skip(
                    bot_id, shared_dir, app_id=app_id, now=now,
                )
                if budget_skip:
                    apps_first_audit_deferred += 1
                    logger.info(
                        "audit: pausing first Tier-3 audit of %s/%s — %s",
                        bot_id, app_id, budget_reason,
                    )
                    _append_trail(audits_dir, app_id, {
                        "ts": _iso_now(),
                        "kind": "audit_budget_paused",
                        "tier": 3,
                        "audit_run_id": run_id,
                        "reason": budget_reason,
                    })
                    continue

        # ── Dispatch Stage 3a + 3b ─────────────────────────────────────────
        # shared_dir is required for cost recovery from TurnObserver when
        # the openclaw-agent subprocess hangs after firing the LLM call —
        # without it, timed-out dispatches silently report tokens_used=0
        # even though the call was billed (2026-05-20 bleed, $5+ on team_bot_a).
        #
        # A just-built app's FIRST audit (provisioning) runs on the standard
        # role, not the bot's default (power for full-tier bots). Every later
        # audit passes model=None and keeps the bot default — steady-state
        # audits are untouched. Decision C
        # (internal/finding-new-bot-activation-cost-2026-06-12.md).
        audit_model = (
            _resolve_first_audit_model(bot_id)
            if _is_first_audit(manifest)
            else None
        )
        result = run_tier3_for_app(
            manifest=manifest,
            workspace=workspace,
            bot_id=bot_id,
            audit_run_id=run_id,
            full_audit=full_audit,
            shared_dir=shared_dir,
            model=audit_model,
        )
        apps_audited += 1
        total_tokens += result.tokens_used

        # Build per-finding outbox records, executing auto_fix transformations
        # along the way. Order matters: we cap auto_fix and propose counts
        # per app per spec §5.3 to prevent any single run from spiraling.
        auto_fix_applied = 0
        proposals_raised = 0

        for decision in result.decisions:
            obs = _find_observation(result.observations, decision.obs_id)
            if obs is None:
                continue
            outcome = decision.outcome
            transformation_summary = ""
            conflict: ConflictReport | None = None

            # Calibration mode: demote auto_fix → propose before doing anything.
            if outcome == OUTCOME_AUTO_FIX and calibration_on:
                outcome = OUTCOME_PROPOSE
                transformation_summary = (
                    "auto_fix demoted to propose (calibration_mode=true)"
                )
            # Real auto_fix path (calibration off): try executor.
            elif outcome == OUTCOME_AUTO_FIX:
                if auto_fix_applied >= max_auto_fix:
                    outcome = OUTCOME_PROPOSE
                    transformation_summary = (
                        f"auto_fix demoted to propose: "
                        f"per-run cap reached ({max_auto_fix})"
                    )
                else:
                    ex_out = execute_auto_fix(
                        transformation=decision.transformation,
                        manifest=manifest,
                        workspace=workspace,
                        evidence={"path": _first_path_evidence(obs)},
                        other_manifests=all_manifest_dicts,
                    )
                    if ex_out.conflict is not None:
                        # Cross-app conflict — emit a conflict_notice instead.
                        conflict = ex_out.conflict
                        outcome = "conflict_notice"
                        transformation_summary = ex_out.summary
                    elif ex_out.applied:
                        outcome = OUTCOME_AUTO_FIX
                        auto_fix_applied += 1
                        transformation_summary = ex_out.summary
                    else:
                        # Unknown transformation kind — fall back to propose.
                        outcome = OUTCOME_PROPOSE
                        transformation_summary = ex_out.summary

            # Apply propose cap.
            if outcome == OUTCOME_PROPOSE:
                if proposals_raised >= max_proposals:
                    outcome = OUTCOME_DISMISS
                    transformation_summary = (
                        f"propose demoted to dismiss: per-run cap reached "
                        f"({max_proposals})"
                    )
                else:
                    proposals_raised += 1

            total_outcomes[outcome] = total_outcomes.get(outcome, 0) + 1

            # Outbox record per decision (except dismiss — keep noise out).
            if outcome != OUTCOME_DISMISS:
                if outcome == "conflict_notice" and conflict is not None:
                    _write_outbox_record(outbox_dir, {
                        "record_id": _new_id("rec"),
                        "audit_run_id": run_id,
                        "kind": "conflict_notice",
                        "ts": _iso_now(),
                        "runner_version": RUNNER_VERSION,
                        "producer": "app_audit_tier3",
                        "bot_id": bot_id,
                        "app_id": app_id,
                        "signature": obs.signature(bot_id, app_id),
                        "obs_id": obs.obs_id,
                        "category": obs.category,
                        "severity": obs.severity,
                        "description": obs.description,
                        "evidence": obs.evidence,
                        "rationale": decision.rationale,
                        "summary": transformation_summary,
                        "file_path": conflict.file_path,
                        "affected_apps": conflict.affected_apps,
                    })
                else:
                    _write_outbox_record(outbox_dir, {
                        "record_id": _new_id("rec"),
                        "audit_run_id": run_id,
                        "kind": "tier3_finding",
                        "ts": _iso_now(),
                        "runner_version": RUNNER_VERSION,
                        "producer": "app_audit_tier3",
                        "bot_id": bot_id,
                        "app_id": app_id,
                        "signature": obs.signature(bot_id, app_id),
                        "obs_id": obs.obs_id,
                        "category": obs.category,
                        "severity": obs.severity,
                        "description": obs.description,
                        "evidence": obs.evidence,
                        "outcome": outcome,
                        "rationale": decision.rationale,
                        "transformation_summary": transformation_summary,
                    })

            _append_trail(audits_dir, app_id, {
                "ts": _iso_now(),
                "kind": f"tier3_{outcome}",
                "audit_run_id": run_id,
                "obs_id": obs.obs_id,
                "category": obs.category,
                "severity": obs.severity,
                "signature": obs.signature(bot_id, app_id),
                "rationale": decision.rationale,
                "summary": transformation_summary,
            })

        # Per-app trail + manifest stamp
        outcomes = {
            "dismiss":          sum(1 for d in result.decisions if d.outcome == OUTCOME_DISMISS),
            "auto_fix":         auto_fix_applied,
            "propose":          proposals_raised,
            "conflict_notice":  sum(1 for d in result.decisions if d.outcome == OUTCOME_AUTO_FIX) - auto_fix_applied,
        }
        # ^^ conflict_notice estimate: any auto_fix the LLM picked that didn't
        # land as actually-applied. Best-effort; the per-decision loop above
        # is the authoritative count via total_outcomes.

        stamp = {
            "verified_at": _iso_now(),
            "runner_version": RUNNER_VERSION,
            "audit_run_id": run_id,
            "status": result.status,
            "findings_count": len(result.observations),
            "outcomes": outcomes,
            "tokens": result.tokens_used,
            "full_audit": full_audit,
            "error": result.error,
        }
        _stamp_manifest_field(path, manifest, "last_audit", stamp)

        # Persist the full per-run Stage 3a+3b JSON for forensics.
        _persist_audit_run_json(audits_dir, app_id, run_id, result.to_dict())

        _append_trail(audits_dir, app_id, {
            "ts": _iso_now(),
            "kind": "audit_run",
            "tier": 3,
            "audit_run_id": run_id,
            "status": result.status,
            "findings_count": len(result.observations),
            "outcomes": outcomes,
            "tokens": result.tokens_used,
            "full_audit": full_audit,
        })

        # Bail if we've blown the per-tick token ceiling — defer rest to next tick.
        if total_tokens > max_tokens * 10:   # 10× across all apps in one tick
            break

    # ── Run summary record ─────────────────────────────────────────────────
    completed_at = _iso_now()
    _write_outbox_record(outbox_dir, {
        "record_id": _new_id("rec"),
        "audit_run_id": run_id,
        "kind": "tier3_run_summary",
        "ts": completed_at,
        "runner_version": RUNNER_VERSION,
        "bot_id": bot_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "apps_audited": apps_audited,
        "apps_skipped_ineligible": apps_skipped_ineligible,
        "apps_skipped_not_due": apps_skipped_not_due,
        "apps_first_audit_deferred": apps_first_audit_deferred,
        "outcomes": total_outcomes,
        "total_tokens": total_tokens,
        "full_audit": full_audit,
    })

    return {
        "audit_run_id": run_id,
        "apps_audited": apps_audited,
        "apps_skipped_ineligible": apps_skipped_ineligible,
        "apps_skipped_not_due": apps_skipped_not_due,
        "apps_first_audit_deferred": apps_first_audit_deferred,
        "outcomes": total_outcomes,
        "total_tokens": total_tokens,
    }


# ── Skill audit run (Workstream B-skills) ───────────────────────────────────


def run_skill_audits(
    workspace: Path,
    *,
    bot_id: str,
    shared_dir: Path,
    audit_run_id: str | None = None,
    only_skills: list[str] | None = None,
    full_audit: bool = False,
    force_due: bool = False,
) -> dict:
    """Run skill audits across this bot's installed skills.

    Mirrors :func:`run_tier3` but scoped to skills. For each skill we
    assemble local state, look up the install module to derive the
    synthetic contract, run Stage 3a + 3b, and emit outbox records of
    kind ``skill_finding`` (plus a ``skill_run_summary``). Per-skill
    trails land under ``{workspace}/evolve/skill_audits/<skill>/trail.jsonl``.
    """
    # Local imports — keep them out of module load so the runner can do
    # tier-2-only work on bots that haven't yet deployed the new modules.
    from skill_audit import run_skill_audit, find_install_module
    from substrate_audit_state import (
        KNOWN_SKILLS, assemble_skill_state, gather_recent_skill_failures,
    )

    run_id = audit_run_id or _new_id("skill-run")
    started_at = _iso_now()
    outbox_dir = _audit_outbox_dir(workspace)
    trails_root = _skill_audits_dir(workspace)
    trails_root.mkdir(parents=True, exist_ok=True)

    pod_config = _load_pod_config(workspace)
    skill_cfg = pod_config.get("skill_audit", {}) or {}
    calibration_on = bool(skill_cfg.get("calibration_mode", True))
    ceilings = skill_cfg.get("ceilings", {}) or {}
    max_proposals = int(ceilings.get("max_proposals_per_run", 5))
    max_tokens = int(ceilings.get("max_tokens_per_audit", 100_000))

    targets = list(only_skills) if only_skills else list(KNOWN_SKILLS)
    total_tokens = 0
    skills_audited = 0
    outcomes_total = {"dismiss": 0, "auto_fix": 0, "propose": 0}

    for skill_id in targets:
        install_path = find_install_module(skill_id)
        if install_path is None:
            # Not every bot has every skill installed; skip silently.
            continue

        # Recent failures via shared signal store. Best-effort; missing
        # store == empty list.
        recent_failures = gather_recent_skill_failures(
            skill_id=skill_id, bot_id=bot_id, shared_dir=shared_dir,
        )
        state = assemble_skill_state(
            skill_id=skill_id, bot_id=bot_id, home=Path.home(),
            recent_failures=recent_failures,
        )

        # Accepted-signatures sidecar — substrate elements don't carry
        # manifests, so the accepted list lives next to the trail.
        accepted_set = _load_accepted_signatures(
            trails_root / skill_id / "accepted.json",
        ) if not full_audit else set()

        # Trail tail so the LLM sees what changed since the last audit.
        trail_tail = _read_trail_tail(
            trails_root / skill_id / "trail.jsonl", limit=30,
        )

        result = run_skill_audit(
            skill_id=skill_id, bot_id=bot_id, state=state,
            install_module_path=install_path,
            audit_run_id=run_id, full_audit=full_audit,
            accepted_signatures=accepted_set,
            trail_tail=trail_tail,
            shared_dir=shared_dir,
        )
        skills_audited += 1
        total_tokens += result.tokens_used

        proposals_raised = 0
        run_outcomes = {"dismiss": 0, "auto_fix": 0, "propose": 0}

        for decision in result.decisions:
            obs = next(
                (o for o in result.observations if o.obs_id == decision.obs_id),
                None,
            )
            if obs is None:
                continue
            outcome = decision.outcome
            note = ""

            # Calibration mode: demote auto_fix → propose at the
            # orchestration layer (spec §5.1). v1 ships calibration-on.
            if outcome == OUTCOME_AUTO_FIX and calibration_on:
                outcome = OUTCOME_PROPOSE
                note = "auto_fix demoted to propose (calibration_mode=true)"

            # Per-run rate ceiling on proposals.
            if outcome == OUTCOME_PROPOSE:
                if proposals_raised >= max_proposals:
                    outcome = OUTCOME_DISMISS
                    note = (
                        f"propose demoted to dismiss: per-run cap reached "
                        f"({max_proposals})"
                    )
                else:
                    proposals_raised += 1

            run_outcomes[outcome] = run_outcomes.get(outcome, 0) + 1
            outcomes_total[outcome] = outcomes_total.get(outcome, 0) + 1

            if outcome != OUTCOME_DISMISS:
                _write_outbox_record(outbox_dir, {
                    "record_id": _new_id("rec"),
                    "audit_run_id": run_id,
                    "kind": "skill_finding",
                    "ts": _iso_now(),
                    "runner_version": RUNNER_VERSION,
                    "producer": "skill_audit",
                    "bot_id": bot_id,
                    "skill_id": skill_id,
                    "signature": obs.signature(bot_id, skill_id),
                    "obs_id": obs.obs_id,
                    "category": obs.category,
                    "severity": obs.severity,
                    "description": obs.description,
                    "evidence": obs.evidence,
                    "outcome": outcome,
                    "rationale": decision.rationale,
                    "transformation_summary": note,
                })

            _append_trail(trails_root, skill_id, {
                "ts": _iso_now(),
                "kind": f"skill_{outcome}",
                "audit_run_id": run_id,
                "obs_id": obs.obs_id,
                "category": obs.category,
                "severity": obs.severity,
                "signature": obs.signature(bot_id, skill_id),
                "rationale": decision.rationale,
                "summary": note,
            })

        # Per-skill summary trail entry — always written, even when no
        # findings, so operators can see audits ran.
        _append_trail(trails_root, skill_id, {
            "ts": _iso_now(),
            "kind": "audit_run",
            "element_type": "skill",
            "audit_run_id": run_id,
            "status": result.status,
            "findings_count": len(result.observations),
            "outcomes": run_outcomes,
            "tokens": result.tokens_used,
            "full_audit": full_audit,
            "error": result.error or None,
        })

        # Persist the full per-run JSON for forensics.
        _persist_substrate_run_json(
            trails_root / skill_id, run_id, result.to_dict(),
        )

        if total_tokens > max_tokens * 10:
            # Pod-tick ceiling — defer the rest to the next tick.
            break

    completed_at = _iso_now()
    _write_outbox_record(outbox_dir, {
        "record_id": _new_id("rec"),
        "audit_run_id": run_id,
        "kind": "skill_run_summary",
        "ts": completed_at,
        "runner_version": RUNNER_VERSION,
        "bot_id": bot_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "skills_audited": skills_audited,
        "outcomes": outcomes_total,
        "total_tokens": total_tokens,
        "full_audit": full_audit,
    })

    return {
        "audit_run_id": run_id,
        "skills_audited": skills_audited,
        "outcomes": outcomes_total,
        "total_tokens": total_tokens,
    }


# ── Provider audit run (Workstream B-skills) ────────────────────────────────


def run_provider_audits(
    workspace: Path,
    *,
    bot_id: str,
    shared_dir: Path,
    audit_run_id: str | None = None,
    only_providers: list[str] | None = None,
    full_audit: bool = False,
    force_due: bool = False,
) -> dict:
    """Run OAuth-provider audits across this bot's configured providers.

    Same orchestration as :func:`run_skill_audits`. Outputs land under
    ``{workspace}/evolve/provider_audits/<provider>/trail.jsonl`` and the
    outbox kind is ``provider_finding``.
    """
    from provider_audit import run_provider_audit, find_provider_module
    from substrate_audit_state import (
        KNOWN_PROVIDERS, assemble_provider_state, gather_recent_skill_failures,
    )

    run_id = audit_run_id or _new_id("provider-run")
    started_at = _iso_now()
    outbox_dir = _audit_outbox_dir(workspace)
    trails_root = _provider_audits_dir(workspace)
    trails_root.mkdir(parents=True, exist_ok=True)

    pod_config = _load_pod_config(workspace)
    provider_cfg = pod_config.get("provider_audit", {}) or {}
    calibration_on = bool(provider_cfg.get("calibration_mode", True))
    ceilings = provider_cfg.get("ceilings", {}) or {}
    max_proposals = int(ceilings.get("max_proposals_per_run", 5))
    max_tokens = int(ceilings.get("max_tokens_per_audit", 100_000))

    targets = list(only_providers) if only_providers else list(KNOWN_PROVIDERS)
    total_tokens = 0
    providers_audited = 0
    outcomes_total = {"dismiss": 0, "auto_fix": 0, "propose": 0}

    for provider_id in targets:
        module_path = find_provider_module(provider_id)
        if module_path is None:
            continue

        recent_failures = gather_recent_skill_failures(
            skill_id=provider_id, bot_id=bot_id, shared_dir=shared_dir,
        )
        state = assemble_provider_state(
            provider_id=provider_id, bot_id=bot_id, home=Path.home(),
            recent_failures=recent_failures,
        )

        accepted_set = _load_accepted_signatures(
            trails_root / provider_id / "accepted.json",
        ) if not full_audit else set()
        trail_tail = _read_trail_tail(
            trails_root / provider_id / "trail.jsonl", limit=30,
        )

        result = run_provider_audit(
            provider_id=provider_id, bot_id=bot_id, state=state,
            provider_module_path=module_path,
            audit_run_id=run_id, full_audit=full_audit,
            accepted_signatures=accepted_set,
            trail_tail=trail_tail,
            shared_dir=shared_dir,
        )
        providers_audited += 1
        total_tokens += result.tokens_used

        proposals_raised = 0
        run_outcomes = {"dismiss": 0, "auto_fix": 0, "propose": 0}

        for decision in result.decisions:
            obs = next(
                (o for o in result.observations if o.obs_id == decision.obs_id),
                None,
            )
            if obs is None:
                continue
            outcome = decision.outcome
            note = ""

            if outcome == OUTCOME_AUTO_FIX and calibration_on:
                outcome = OUTCOME_PROPOSE
                note = "auto_fix demoted to propose (calibration_mode=true)"

            if outcome == OUTCOME_PROPOSE:
                if proposals_raised >= max_proposals:
                    outcome = OUTCOME_DISMISS
                    note = (
                        f"propose demoted to dismiss: per-run cap reached "
                        f"({max_proposals})"
                    )
                else:
                    proposals_raised += 1

            run_outcomes[outcome] = run_outcomes.get(outcome, 0) + 1
            outcomes_total[outcome] = outcomes_total.get(outcome, 0) + 1

            if outcome != OUTCOME_DISMISS:
                _write_outbox_record(outbox_dir, {
                    "record_id": _new_id("rec"),
                    "audit_run_id": run_id,
                    "kind": "provider_finding",
                    "ts": _iso_now(),
                    "runner_version": RUNNER_VERSION,
                    "producer": "provider_audit",
                    "bot_id": bot_id,
                    "provider_id": provider_id,
                    "signature": obs.signature(bot_id, provider_id),
                    "obs_id": obs.obs_id,
                    "category": obs.category,
                    "severity": obs.severity,
                    "description": obs.description,
                    "evidence": obs.evidence,
                    "outcome": outcome,
                    "rationale": decision.rationale,
                    "transformation_summary": note,
                })

            _append_trail(trails_root, provider_id, {
                "ts": _iso_now(),
                "kind": f"provider_{outcome}",
                "audit_run_id": run_id,
                "obs_id": obs.obs_id,
                "category": obs.category,
                "severity": obs.severity,
                "signature": obs.signature(bot_id, provider_id),
                "rationale": decision.rationale,
                "summary": note,
            })

        _append_trail(trails_root, provider_id, {
            "ts": _iso_now(),
            "kind": "audit_run",
            "element_type": "provider",
            "audit_run_id": run_id,
            "status": result.status,
            "findings_count": len(result.observations),
            "outcomes": run_outcomes,
            "tokens": result.tokens_used,
            "full_audit": full_audit,
            "error": result.error or None,
        })

        _persist_substrate_run_json(
            trails_root / provider_id, run_id, result.to_dict(),
        )

        if total_tokens > max_tokens * 10:
            break

    completed_at = _iso_now()
    _write_outbox_record(outbox_dir, {
        "record_id": _new_id("rec"),
        "audit_run_id": run_id,
        "kind": "provider_run_summary",
        "ts": completed_at,
        "runner_version": RUNNER_VERSION,
        "bot_id": bot_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "providers_audited": providers_audited,
        "outcomes": outcomes_total,
        "total_tokens": total_tokens,
        "full_audit": full_audit,
    })

    return {
        "audit_run_id": run_id,
        "providers_audited": providers_audited,
        "outcomes": outcomes_total,
        "total_tokens": total_tokens,
    }


# ── Substrate helpers (shared by skill + provider runs) ─────────────────────


def _load_accepted_signatures(path: Path) -> set[str]:
    """Load operator-accepted finding signatures from a sidecar JSON file.

    Skills + providers don't have per-element manifests like apps, so we
    keep the accepted list in a small JSON file alongside the trail.
    Shape: ``{"accepted": [{"signature": str, ...}, ...]}``. Returns an
    empty set when the file is missing or malformed.
    """
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    accepted = data.get("accepted") or []
    return {
        a.get("signature") for a in accepted
        if isinstance(a, dict) and a.get("signature")
    }


def _read_trail_tail(path: Path, *, limit: int = 30) -> list[dict]:
    """Last N lines of a trail.jsonl as parsed dicts. Tolerant of malformed."""
    if not path.exists():
        return []
    try:
        lines = path.read_text().splitlines()[-limit:]
    except OSError:
        return []
    out: list[dict] = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def _persist_substrate_run_json(
    element_dir: Path, run_id: str, payload: dict,
) -> Path:
    """Persist full per-run JSON for forensics — parallel to apps' per-run JSONs."""
    element_dir.mkdir(parents=True, exist_ok=True)
    p = element_dir / f"{run_id}.json"
    p.write_text(json.dumps(payload, indent=2))
    return p


def _find_observation(observations: list[Observation], obs_id: str) -> Observation | None:
    for o in observations:
        if o.obs_id == obs_id:
            return o
    return None


def _first_path_evidence(obs: Observation) -> str:
    """Best-effort: pull a file path out of an observation's evidence list."""
    for ref in obs.evidence:
        if "/" in ref or ref.endswith((".py", ".sh", ".md", ".json")):
            return ref.split(":", 1)[0]
    return ""


def _persist_audit_run_json(audits_dir: Path, app_id: str, run_id: str, payload: dict) -> Path:
    app_dir = audits_dir / app_id
    app_dir.mkdir(parents=True, exist_ok=True)
    out = app_dir / f"{run_id}.json"
    out.write_text(json.dumps(payload, indent=2))
    return out


# ── Inbox pickup ────────────────────────────────────────────────────────────


def process_inbox(
    workspace: Path,
    *,
    bot_id: str,
    shared_dir: Path,
    request_id: str | None = None,
) -> dict:
    """Drain queued audit requests from audit_inbox/.

    Each inbox file is a JSON dict:
      {
        "request_id": str,
        "kind": "tier3_audit",
        "apps": ["app_id_1", "app_id_2"] | "all",   # which apps to audit
        "full_audit": bool,                          # ignore audit_accepted[]
        "requested_by": str,                         # CLI / UI / evo:user_key
        "requested_at": str (ISO),
      }

    When request_id is provided, only that one request is processed (the
    admin's --pickup-inbox kick targets a specific request). Otherwise we
    drain every queued request in the inbox.

    Inbox files are moved to audit_inbox/_ingested/<date>/ after processing.
    """
    inbox_dir = _audit_inbox_dir(workspace)
    inbox_dir.mkdir(parents=True, exist_ok=True)

    targets: list[Path] = []
    if request_id:
        candidate = inbox_dir / f"{request_id}.json"
        if candidate.exists():
            targets = [candidate]
    else:
        targets = [
            p for p in sorted(inbox_dir.iterdir())
            if p.is_file() and p.suffix == ".json" and not p.name.startswith(".")
            and p.name != "_ingested"
        ]

    processed = 0
    errors: list[str] = []
    for path in targets:
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"unreadable inbox file {path.name}: {exc}")
            _archive_inbox_file(path, workspace)
            continue
        try:
            _execute_request(data, workspace, bot_id, shared_dir)
            processed += 1
        except Exception as exc:
            errors.append(f"request {data.get('request_id', '?')} failed: {exc}")
        _archive_inbox_file(path, workspace)

    return {"processed": processed, "errors": errors}


def _execute_request(
    request: dict, workspace: Path, bot_id: str, shared_dir: Path,
) -> None:
    kind = request.get("kind") or "tier3_audit"
    full_audit = bool(request.get("full_audit", False))

    if kind == "tier3_audit":
        apps_arg = request.get("apps", "all")
        only_apps = None if apps_arg == "all" else list(apps_arg or [])
        run_tier3(
            workspace,
            bot_id=bot_id,
            shared_dir=shared_dir,
            audit_run_id=request.get("request_id") or _new_id("tier3-run"),
            only_apps=only_apps,
            full_audit=full_audit,
            force_due=True,
        )
    elif kind == "skill_audit":
        # Workstream B-skills. Inbox may carry "skills": ["gmail", ...] or "all".
        skills_arg = request.get("skills", request.get("apps", "all"))
        only_skills = None if skills_arg == "all" else list(skills_arg or [])
        run_skill_audits(
            workspace,
            bot_id=bot_id,
            shared_dir=shared_dir,
            audit_run_id=request.get("request_id") or _new_id("skill-run"),
            only_skills=only_skills,
            full_audit=full_audit,
            force_due=True,
        )
    elif kind == "provider_audit":
        providers_arg = request.get("providers", request.get("apps", "all"))
        only_providers = None if providers_arg == "all" else list(providers_arg or [])
        run_provider_audits(
            workspace,
            bot_id=bot_id,
            shared_dir=shared_dir,
            audit_run_id=request.get("request_id") or _new_id("provider-run"),
            only_providers=only_providers,
            full_audit=full_audit,
            force_due=True,
        )
    elif kind == "investigation":
        # Workstream C — ``evo fail <description>`` investigation.
        # Routed to a separate runner so the contract-audit code paths
        # stay focused on their own concerns. The investigation runner
        # writes its own outbox record (``investigation_diagnosis``)
        # which the admin's poller turns into a notification — NOT a
        # Proposal (per direct-reply design, spec §5).
        run_investigation_request(
            workspace,
            bot_id=bot_id,
            shared_dir=shared_dir,
            request=request,
        )
    else:
        # Future: support "tier2_audit" / "infra_audit" / etc. For now,
        # ignore so unknown kinds don't crash the runner — they archive
        # out of the inbox normally.
        pass


# ── Investigation run (Workstream C — ``evo fail``) ──────────────────────────


def run_investigation_request(
    workspace: Path,
    *,
    bot_id: str,
    shared_dir: Path,
    request: dict,
) -> dict:
    """Execute a single ``evo fail`` investigation request.

    Reads user_description + requesting_user + requested_at from the
    inbox file, runs the two-stage investigation, then writes:
      - the structured per-run JSON under investigations/<id>.json
      - one line in investigations/trail.jsonl
      - one outbox record of kind ``investigation_diagnosis``

    The runner never writes a Proposal here — by design, diagnoses land
    directly in the user's notification queue. The escalation path
    (``evo fail flag``) writes ``investigation_unresolved`` admin-side.

    See spec-audit-extensions-2026-05-17.md §5.2 / §5.6.
    """
    # Local import — defers analyzer-package imports so a bot that doesn't
    # have the investigation module yet (mid-rollout) still loads the
    # rest of the runner.
    from app_audit_investigation import (  # noqa: E402
        render_investigation_trail_entry,
        render_outbox_record,
        run_investigation,
    )

    investigation_id = (
        request.get("investigation_id")
        or request.get("request_id")
        or _new_id("inv")
    )
    user_description = (request.get("user_description") or "").strip()
    requesting_user = (request.get("requesting_user") or "operator").strip()
    requested_at = request.get("requested_at") or _iso_now()

    out = run_investigation(
        investigation_id=investigation_id,
        bot_id=bot_id,
        workspace=workspace,
        shared_dir=shared_dir,
        user_description=user_description,
        requesting_user=requesting_user,
        requested_at=requested_at,
    )

    # Persist per-run JSON for forensics (parallel to per-app audit JSONs).
    inv_dir = _investigations_dir(workspace)
    inv_dir.mkdir(parents=True, exist_ok=True)
    try:
        (inv_dir / f"{investigation_id}.json").write_text(
            json.dumps(out.to_dict(), indent=2)
        )
    except OSError as exc:
        logger.warning("investigation: per-run JSON write failed: %s", exc)

    # Append trail entry.
    try:
        trail_path = inv_dir / "trail.jsonl"
        with trail_path.open("a") as fh:
            fh.write(json.dumps(render_investigation_trail_entry(out)) + "\n")
    except OSError as exc:
        logger.warning("investigation: trail append failed: %s", exc)

    # Outbox record — the admin's poller picks this up and routes it to
    # the requesting user's notification queue.
    outbox_dir = _audit_outbox_dir(workspace)
    record = render_outbox_record(out, runner_version=RUNNER_VERSION)
    _write_outbox_record(outbox_dir, record)

    # Retention prune — 30 days per spec §8 Q2. Best-effort; we prune at
    # the end of every investigation so we don't need a separate cron.
    try:
        _prune_investigations(inv_dir, retain_days=30)
    except OSError as exc:
        logger.warning("investigation: prune failed: %s", exc)

    return out.to_dict()


def _prune_investigations(inv_dir: Path, *, retain_days: int) -> int:
    """Drop investigation JSONs older than retain_days. Returns count pruned.

    Doesn't touch trail.jsonl beyond a soft cap: when trail exceeds 1000
    lines we keep the most recent 500. Best-effort throughout; never
    raises.
    """
    pruned = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retain_days)
    if not inv_dir.exists():
        return 0
    for f in inv_dir.iterdir():
        if not f.is_file() or f.suffix != ".json":
            continue
        try:
            stat = f.stat()
        except OSError:
            continue
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            try:
                f.unlink()
                pruned += 1
            except OSError:
                continue

    trail = inv_dir / "trail.jsonl"
    if trail.exists():
        try:
            lines = trail.read_text().splitlines()
            if len(lines) > 1000:
                trail.write_text("\n".join(lines[-500:]) + "\n")
        except OSError:
            pass

    return pruned


def _archive_inbox_file(path: Path, workspace: Path) -> None:
    """Move processed inbox file under _ingested/<date>/. Best-effort."""
    today = _iso_now().split("T")[0]
    ingested_dir = _audit_inbox_ingested(workspace) / today
    try:
        ingested_dir.mkdir(parents=True, exist_ok=True)
        path.rename(ingested_dir / path.name)
    except OSError:
        try:
            path.unlink()
        except OSError:
            pass


def _stamp_manifest_field(path: Path, manifest: dict, field_name: str, value: dict) -> bool:
    """Atomically update a single top-level field on the manifest JSON.

    Used for both last_structural_verify (Tier 2) and last_audit (Tier 3).
    """
    manifest[field_name] = value
    try:
        tmp = path.with_suffix(f".tmp-{os.getpid()}")
        tmp.write_text(json.dumps(manifest, indent=2))
        os.replace(str(tmp), str(path))
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def _status_for(findings: list[Finding]) -> str:
    if not findings:
        return "ok"
    severities = {f.severity for f in findings}
    if "critical" in severities:
        return "failed"
    if "major" in severities:
        return "warning"
    return "ok_with_minor"


def _by_severity(findings: list[Finding]) -> dict[str, int]:
    counts = {"critical": 0, "major": 0, "minor": 0, "info": 0}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return counts


# ── Fit Reviewer sibling pass (Bite 3 wiring) ───────────────────────────────


def _maybe_run_fit_review(
    workspace: Path, *, bot_id: str, shared_dir: Path,
) -> None:
    """Sibling pass: run the Fit Reviewer (Bite 3) on its own weekly cadence.

    Piggybacks the hourly Tier-3 audit tick so the Fit Reviewer needs **NO new
    launchd job** (spec-fit-reviewer §8: "a sibling pass in the same bot-side
    scheduler, reusing the outbox/poller plumbing"). The pass is cadence-gated by
    its own per-bot sentinel via ``run_if_due`` (weekly), shares this run's audit
    lock (so at most one in-bot LLM pass runs at a time per bot), and is fully
    isolated: any failure is logged and swallowed — a Fit Reviewer problem must
    never abort or red an audit run. Lazy import so a bot mid-rollout that hasn't
    deployed the ``fit_review`` package yet still runs audits.
    """
    try:
        from fit_review.runner import run_if_due

        result = run_if_due(workspace, bot_id=bot_id, shared_dir=shared_dir)
        if result.get("ran"):
            logger.info(
                "fit_review: sibling pass ran · decision=%s wrote_candidate=%s",
                result.get("decision"),
                result.get("wrote_candidate"),
            )
    except Exception as exc:  # noqa: BLE001 — never let it abort the audit
        logger.warning("fit_review: sibling pass failed (non-fatal): %s", exc)


# ── Entry point ─────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="App audit runner (Tier 2 + Tier 3)")
    ap.add_argument("--bot-id", required=True,
                    help="Logical bot id (may differ from macOS user)")
    ap.add_argument("--shared-dir", default="/Users/Shared/evolve",
                    help="Path to pod-wide shared directory")
    ap.add_argument("--tier", default="2", choices=["2", "3"],
                    help="Audit tier to run (when --kind=app)")
    # --kind: dispatch to app / skill / provider audit. Defaults to app for
    # backward compat with existing LaunchDaemon plists (Workstream B-skills).
    ap.add_argument("--kind", default=KIND_APP, choices=list(VALID_KINDS),
                    help="Audit element type (default: app)")
    ap.add_argument("--audit-run-id", default=None,
                    help="Override audit run id (for on-demand requests)")
    # --pickup-inbox: drain queued audit requests instead of running a fresh
    # cadence-driven pass. The admin's --kick path writes a request file then
    # invokes this mode so the bot picks it up immediately rather than
    # waiting for the hourly cron tick.
    ap.add_argument("--pickup-inbox", action="store_true",
                    help="Drain audit_inbox/ instead of running cadence-driven pass")
    # --repair: drain audit_inbox/ for repair-*.json requests (operator
    # clicked Repair on a chip). Mutually exclusive with --tier 3 and
    # --pickup-inbox in the sense that we early-return after handling.
    # See packages/analyzer/app_repair_runner.py for the session driver
    # and spec-app-coherence-and-reconciliation-2026-06-05.md §11.3.
    ap.add_argument("--repair", action="store_true",
                    help="Drain audit_inbox/ for repair-*.json requests")
    ap.add_argument("--request-id", default=None,
                    help="When set with --pickup-inbox or --repair, process only that request")
    args = ap.parse_args(argv)

    if args.repair and args.pickup_inbox:
        print("[audit_runner] --repair and --pickup-inbox are mutually exclusive",
              file=sys.stderr)
        return 2

    workspace = _bot_workspace()
    shared_dir = Path(args.shared_dir)

    # Ensure outbox + audits dirs exist before locking — the lock itself
    # lives under workspace/evolve/, which mkdir'd here.
    _evolve_dir(workspace).mkdir(parents=True, exist_ok=True)
    _audit_outbox_dir(workspace).mkdir(parents=True, exist_ok=True)
    _audit_inbox_dir(workspace).mkdir(parents=True, exist_ok=True)
    _audits_dir(workspace).mkdir(parents=True, exist_ok=True)
    _skill_audits_dir(workspace).mkdir(parents=True, exist_ok=True)
    _provider_audits_dir(workspace).mkdir(parents=True, exist_ok=True)
    _investigations_dir(workspace).mkdir(parents=True, exist_ok=True)

    try:
        lock_fh = _acquire_lock(workspace)
    except LockBusy:
        print(f"[audit_runner] lock held by another invocation; exiting cleanly",
              file=sys.stderr)
        return 0

    try:
        if args.repair:
            # Lazy import — keeps the rest of the runner usable on bots
            # where the repair_runner module hasn't been deployed yet.
            from app_repair_runner import process_repair_inbox

            result = process_repair_inbox(
                workspace,
                bot_id=args.bot_id,
                shared_dir=shared_dir,
                request_id=args.request_id,
            )
            print(
                f"[audit_runner] repair done · {result['processed']} requests · "
                f"applied={result['applied']} failed={result['failed']}"
                + (f" · errors={result['errors']}" if result['errors'] else "")
            )
            return 0

        if args.pickup_inbox:
            result = process_inbox(
                workspace,
                bot_id=args.bot_id,
                shared_dir=shared_dir,
                request_id=args.request_id,
            )
            print(
                f"[audit_runner] pickup-inbox done · {result['processed']} requests"
                + (f" · errors={result['errors']}" if result['errors'] else "")
            )
            return 0

        # Substrate audit kinds (Workstream B-skills) — skill / provider.
        # The --tier arg is irrelevant when --kind != app; we always run
        # the equivalent of tier-3 for substrate elements.
        if args.kind == KIND_SKILL:
            result = run_skill_audits(
                workspace,
                bot_id=args.bot_id,
                shared_dir=shared_dir,
                audit_run_id=args.audit_run_id,
            )
            print(
                f"[audit_runner] skill done · {result['skills_audited']} audited · "
                f"outcomes={result['outcomes']} · tokens={result['total_tokens']}"
            )
            return 0

        if args.kind == KIND_PROVIDER:
            result = run_provider_audits(
                workspace,
                bot_id=args.bot_id,
                shared_dir=shared_dir,
                audit_run_id=args.audit_run_id,
            )
            print(
                f"[audit_runner] provider done · {result['providers_audited']} audited · "
                f"outcomes={result['outcomes']} · tokens={result['total_tokens']}"
            )
            return 0

        # ── App-audit path (default; backward compat) ──────────────────────
        if args.tier == "3":
            result = run_tier3(
                workspace,
                bot_id=args.bot_id,
                shared_dir=shared_dir,
                audit_run_id=args.audit_run_id,
            )
            print(
                f"[audit_runner] tier3 done · {result['apps_audited']} audited · "
                f"{result['apps_skipped_not_due']} not-due · "
                f"{result['apps_first_audit_deferred']} first-audit-deferred · "
                f"{result['apps_skipped_ineligible']} ineligible · "
                f"outcomes={result['outcomes']} · tokens={result['total_tokens']}"
            )
            # Sibling pass: Fit Reviewer (weekly; rides this hourly tick so there
            # is no new launchd job). Cadence-gated, lock already held, isolated.
            _maybe_run_fit_review(
                workspace, bot_id=args.bot_id, shared_dir=shared_dir,
            )
            return 0

        result = run_tier2(
            workspace,
            bot_id=args.bot_id,
            shared_dir=shared_dir,
            audit_run_id=args.audit_run_id,
        )
        print(
            f"[audit_runner] tier2 done · {result['apps_audited']} apps · "
            f"{result['total_findings']} findings "
            f"({result['by_severity']})"
        )
        return 0
    except Exception as exc:
        # Last-ditch trail entry so failed runs surface in the UI rather
        # than silently going missing.
        try:
            tier_label = 3 if args.tier == "3" else 2
            _append_trail(_audits_dir(workspace), "_runner", {
                "ts": _iso_now(),
                "kind": "audit_run",
                "tier": tier_label,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            })
        except OSError:
            pass
        print(f"[audit_runner] FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            lock_fh.close()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
