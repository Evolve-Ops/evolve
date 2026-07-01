"""Pairing auto-approver — pod-admin + auto_admit + block-aware.

The 2026-05-29 spec shipped an *inline* sweep in ``routes_bot_users``
(``_auto_approve_inline``) that auto-approves pod-admin claims when an
admin views the Users page. That covered the "freshly-paired admin
sees their /start already-approved when they land on the page"
ergonomic, but only fires when an operator is actively watching.

This module is the shared implementation reused by both the inline
GET-time sweep and the periodic launchd job
``ai.evolve.evolve.pairing-sweep`` (StartInterval=30s). It honors three
auto-approval triggers, each consulted in order:

1. **Pod-admin claim** (existing) — request id is in
   ``network.json::pod.admins.external_ids[<channel>]``. Auto-approval
   reason: ``"known pod admin"``.

2. **Primary-owner claim** (new) — request id matches
   ``network.json::bots.<bot>.primary_user.external_ids[<channel>]``.
   The primary owner of a bot is the natural ``primary_user`` role
   per spec-user-roster-and-roles-2026-06-07; auto-approving them on
   /start saves the operator a manual step on bootstrap. Skipped when
   ``newcomer_mode == "closed"`` so a sensitive bot stays fully
   operator-controlled.

3. **Per-channel auto_admit mode** (new) — the overlay's per-channel
   ``newcomer_mode == "auto_admit"`` auto-approves any pending pairing
   on that channel. The trusted-private-group case from
   spec-user-roster-and-roles-2026-06-07 §11.

Each pass also enforces:

- **Block index check** — an identity in the overlay's ``blocked`` map
  is never auto-approved regardless of trigger. The block index is
  sticky and overrides auto_admit; a previously-blocked user who
  re-pairs lands in pending and stays there until the operator
  explicitly unblocks and approves.

- **No re-approval of already-approved IDs.** ``_approve`` is
  idempotent at the file level (set semantics on allowFrom), but we
  skip pending entries whose id is already in allowFrom to keep audit
  logs clean.

Spec: docs/spec-user-roster-and-roles-2026-06-07.md §11, deferred
"Phase 1.1 ambient sweep" from docs/spec-per-bot-users-management-2026-05-29.md.
"""

from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..runtime import JobSpec, get_scheduler, render_launchd_plist
from .. import roster_overlay as ro


log = logging.getLogger(__name__)


# ── LaunchDaemon plist + install ──────────────────────────────────────────


SWEEP_LABEL = "ai.evolve.evolve.pairing-sweep"
SWEEP_PLIST = f"/Library/LaunchDaemons/{SWEEP_LABEL}.plist"
SWEEP_INTERVAL_SECONDS = 30
"""30 seconds matches the 2026-05-29 spec's Phase 1.1 target cadence.

Trades some idle wakeups (~2,880/day at the no-work rate) for the
freshly-paired-via-auto_admit user seeing a near-instant welcome.
Auto-approve work is cheap when the pending list is empty (one
``_read_json_or_none`` per channel per bot, returning None and
short-circuiting) — measured ~5ms per sweep on the deployed pod's
8-bot config. Bumping the interval is fine if anyone ever cares.
"""


@dataclass
class SweepResult:
    """Per-bot summary returned by ``run_one_pass`` for testing + logging."""

    bot_id: str
    approved: list[dict[str, Any]] = field(default_factory=list)
    """Each entry: ``{channel, id, reason}``."""

    skipped_blocked: list[dict[str, Any]] = field(default_factory=list)
    """Pending IDs the sweep saw but did not approve due to block index."""

    errors: list[str] = field(default_factory=list)


def run_one_pass(
    network: dict,
    bot_id: str,
    *,
    rbu_module,
) -> SweepResult:
    """Run the auto-approval sweep for a single bot.

    Reads pairing.json + allowFrom.json per channel via the existing
    ``routes_bot_users`` helpers (passed in as ``rbu_module`` to avoid
    a circular import — this module is also imported BY
    routes_bot_users for the inline sweep). For each pending request,
    decides whether to auto-approve based on the three triggers
    enumerated in the module docstring.

    The actual approval call (``rbu_module._approve``) reuses the
    existing write path: pairing.json + allowFrom.json mutation via
    /tmp + sudo cp, identity_cache capture from the pairing meta. So
    the auto-approved user appears with their display name on the
    next render, same as a manually-approved one.

    Failures are caught per-request — one bad pairing doesn't stop
    the sweep for the same bot. Errors land in ``result.errors`` and
    are also log.warning'd for operator visibility.
    """
    result = SweepResult(bot_id=bot_id)

    shared = ro.overlay_path(_shared_dir(network), bot_id).parent.parent
    overlay = ro.load_overlay(shared, bot_id)

    for ch in rbu_module.KNOWN_PROVIDERS:
        try:
            _sweep_channel(network, bot_id, ch, overlay, rbu_module, result)
        except Exception as exc:  # noqa: BLE001
            msg = f"sweep {bot_id}/{ch} raised: {exc}"
            log.warning("%s", msg)
            result.errors.append(msg)

    return result


def _shared_dir(network: dict) -> Path:
    return Path(network.get("sharedDir") or "/Users/Shared/evolve")


def _sweep_channel(
    network: dict,
    bot_id: str,
    channel: str,
    overlay: dict,
    rbu_module,
    result: SweepResult,
) -> None:
    pp = rbu_module._pairing_path(network, bot_id, channel)
    pairing_raw = rbu_module._read_json_or_none(pp)
    if pairing_raw is None:
        return
    requests = pairing_raw.get("requests") or []
    if not requests:
        return

    # Already-approved set so we don't reprocess.
    ap = rbu_module._allowfrom_path(network, bot_id, channel)
    allow_raw = rbu_module._read_json_or_none(ap)
    already_allowed = set((allow_raw or {}).get("allowFrom") or [])

    admin_ids = rbu_module._pod_admin_ids_for(network, channel)
    primary_id = rbu_module._primary_id_for(network, bot_id, channel)
    channel_block = ro.channel_block(overlay, channel)
    mode = channel_block["newcomer_mode"]

    for req in requests:
        ext_id = req.get("id")
        if not ext_id:
            continue
        if ext_id in already_allowed:
            continue  # idempotency — already admitted, leave for cleanup

        # Block index check is unconditional and overrides every
        # auto-approval trigger below. A blocked identity stays in
        # pending until the operator explicitly unblocks.
        if ro.is_blocked(overlay, channel, ext_id):
            result.skipped_blocked.append({
                "channel": channel, "id": ext_id, "reason": "blocked",
            })
            continue

        reason = _resolve_auto_approve_reason(
            ext_id, channel, mode, admin_ids, primary_id)
        if reason is None:
            continue

        try:
            rbu_module._approve(
                network, bot_id, channel, ext_id, req.get("code"))
            result.approved.append({
                "channel": channel, "id": ext_id, "reason": reason,
            })
        except rbu_module._PairingError as exc:
            msg = (f"auto-approve {bot_id}/{channel}/{ext_id} failed: {exc}")
            log.warning("%s", msg)
            result.errors.append(msg)


def _resolve_auto_approve_reason(
    ext_id: str,
    channel: str,
    mode: str,
    admin_ids: "set[str]",
    primary_id: "str | None",
) -> "str | None":
    """Return the trigger string if this id should auto-approve, else None.

    Order matters: pod-admin wins over primary-owner wins over
    per-channel auto_admit. The reason string is the human-readable
    label rendered in the Users UI and the signal-store audit entry.
    """
    if ext_id in admin_ids:
        return "known pod admin"
    if mode != "closed":
        # primary-owner and auto_admit both gated by mode != closed.
        # A "closed" channel stays fully operator-managed; the
        # operator must explicitly approve even bootstrap claims.
        if primary_id and ext_id == primary_id:
            return "bot primary owner"
        if mode == "auto_admit":
            return "channel auto-admit"
    return None


def run_sweep_all_bots(
    network: dict,
    *,
    rbu_module,
) -> list[SweepResult]:
    """Sweep every bot in ``network``. Used by the periodic launchd job."""
    results: list[SweepResult] = []
    for bot_id in (network.get("bots") or {}):
        try:
            results.append(run_one_pass(network, bot_id, rbu_module=rbu_module))
        except Exception as exc:  # noqa: BLE001
            log.warning("sweep %s raised at top level: %s", bot_id, exc)
            results.append(SweepResult(
                bot_id=bot_id, errors=[f"top-level: {exc}"]))
    return results


def _sweep_job_spec(
    evolve_admin_path: str | None = None,
    interval_seconds: int = SWEEP_INTERVAL_SECONDS,
    log_dir: str | None = None,
) -> JobSpec:
    """The pairing-sweep LaunchDaemon spec.

    Mirrors the app-test-scheduler pattern: evolve user, StartInterval,
    --quiet so no-op ticks don't spam the log file (a 30s cadence makes
    2880 ticks/day; without --quiet that's a lot of
    "[pairing-sweep] {...}" lines per day). RunAtLoad=true so a fresh
    install processes anything pending immediately rather than waiting 30s.

    Paths default to the active platform profile (W10-D): macOS renders
    /Users/Shared byte-identically (parity golden under the conftest MACOS
    pin); a Linux pod renders /var/lib, so the systemd unit carries no
    /Users leak.
    """
    from platform_profile import get_profile
    _prof = get_profile()
    if evolve_admin_path is None:
        evolve_admin_path = _prof.venv_evolve_admin
    if log_dir is None:
        log_dir = f"{_prof.shared_dir_default}/logs"
    cmd = " ".join(shlex.quote(a) for a in [
        evolve_admin_path, "pairing-sweep", "--quiet",
    ])
    return JobSpec(
        label=SWEEP_LABEL,
        program_args=["/bin/bash", "-c", f"exec {cmd}"],
        user="evolve",
        start_interval=interval_seconds,
        run_at_load=True,
        stdout_path=f"{log_dir}/pairing-sweep.log",
        stderr_path=f"{log_dir}/pairing-sweep.err.log",
        env={"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
    )


def render_plist(
    evolve_admin_path: str | None = None,
    interval_seconds: int = SWEEP_INTERVAL_SECONDS,
    log_dir: str | None = None,
) -> str:
    """Render the pairing-sweep LaunchDaemon plist as a string.

    Pure — caller writes to disk. See :func:`_sweep_job_spec` for the
    job semantics (incl. the profile-keyed path defaults).
    """
    return render_launchd_plist(_sweep_job_spec(
        evolve_admin_path, interval_seconds, log_dir,
    ))


def install_launchd() -> bool:
    """Install ``ai.evolve.evolve.pairing-sweep``. Idempotent.

    Safe to install on any pod — until the operator sets a
    ``newcomer_mode`` or adds users to the block index, the sweep is
    effectively a no-op beyond the existing pod-admin auto-approval
    (which it now owns from the refactor — the inline GET-time sweep
    delegates to the same module).

    Returns True on install success, False otherwise. Logs failure
    via the module logger so a deploy.py caller can surface it.

    Goes through the Scheduler seam. ``install()`` skips the
    bootout+bootstrap bounce when the on-disk plist is byte-identical;
    that's fine for this 30s-interval daemon (each tick is a fresh
    process — no state to reload), EXCEPT when the job isn't actually
    registered with launchd (an earlier bootstrap failed after the
    plist write, or a manual bootout). The legacy ritual re-registered
    unconditionally, so compensate with remove+install in that case.
    """
    sched = get_scheduler()
    res = sched.install(_sweep_job_spec())
    if res.ok and res.skipped and not sched.status(SWEEP_LABEL)["managed"]:
        sched.remove(SWEEP_LABEL)
        res = sched.install(_sweep_job_spec())
    if not res.ok:
        log.warning("pairing-sweep install: %s", res.message)
        return False
    log.info("pairing-sweep install: bootstrapped %s every %ds",
             SWEEP_LABEL, SWEEP_INTERVAL_SECONDS)
    return True
