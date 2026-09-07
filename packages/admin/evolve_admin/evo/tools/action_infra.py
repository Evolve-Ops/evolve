"""Action tools — infra (pod-wide) daemon lifecycle.

Closes the largest unfilled action gap from the May 2026 evo coverage
audit: when the ``infra_daemon_down`` chip fires (critical severity),
evo could only say "SSH in and kick it" because there was no tool to
act. Now there is.

Mechanism: ``sudo /bin/launchctl kickstart -k system/ai.evolve.<daemon_id>``.
Same code path the operator would type by hand — same as
``action.bot.backup_workspace`` and ``action.bot.restart``, just
targeting a pod-wide infra daemon rather than a per-bot one. The
sudoers grant at ``/etc/sudoers.d/evolve`` already covers the
``system/ai.evolve.*`` wildcard (``setup_wizard._write_evolve_sudoers``
section 8) — no new grant needed.

Daemon whitelist (load-bearing): the tool only accepts daemon ids in
``_ALLOWED_INFRA_DAEMONS``. A model that misreads "evo, restart the
nightly backup" as "restart system/ai.evolve.something_destructive"
gets a clear ``unknown daemon_id`` error rather than a kicked
production system. This is the same defense-in-depth shape as
``action.bot.remove``'s ``confirm: True`` gate.

Risk tier: ``WRITE_RISKY``. Daemon restart is recoverable (launchd
restarts the job) but disrupts whatever was in flight — heal mid-sweep,
verify mid-poll, repo-puller mid-fetch. Auto-runs only under ``auto``
authority; under ``ask`` or ``auto-small`` the proxy renders a confirm
button. The operator sees what's about to restart before it happens.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


# ─── Daemon whitelist ───────────────────────────────────────────────────────
#
# The set of pod-wide infra daemon ids the tool will accept. Each entry
# maps to the launchd label ``ai.evolve.<id>``. We whitelist explicitly
# rather than accepting any ``ai.evolve.*`` label so the model can't
# accidentally restart something destructive (e.g. an experimental job
# that happens to exist on this pod but isn't a documented infra
# daemon).
#
# These are the load-bearing daemons whose down-state triggers
# ``infra_daemon_down`` or related signals, OR whose restart is a known
# operator workflow:
#
#   * evolve.heal — every-5-min gateway probe + recovery. If this is
#     down, downstream bot-status signals go stale.
#   * evolve.verify — applied-proposal verification daemon. If down,
#     applied proposals never advance to verified status.
#   * evolve.repo-puller — auto-pulls origin/main on the deploy
#     checkout every 15min. If down, hotfixes don't land.
#   * evolve.admin-ui — the admin UI itself + tile-metrics + evo
#     gateway. If down, the operator can't reach the UI (so the chip
#     never fires) but the tool still helps when restarting from the
#     primary bot's Telegram surface.
#   * evolve.signal-notifier — every-1-min Signal-transition notifier
#     (alert-notifier Phase 4). If down, alerts don't reach the user.
#   * evolve.pod-health — every-1-min gateway-liveness Signal emitter.
#     If down, pod_health signals don't fire when bots die.
#   * evolve.audit — daily audit sweep. If down, audit findings go stale.
#   * evolve.weekly-review — weekly summary digest job.
#   * evolve.usage-logger — daily usage aggregator into the usage db.
#   * evolve.retention — daily signals/watchdog/proposals cleanup.
#   * evolve.cost_watchdog — every-N-min cost ceiling Signal emitter.
#   * evolve.session_economics — hourly cache-health + engagement Signal.
#   * evolve.embedding_monitor — embedding-spend Signal emitter.
#   * evolve.proposal-auto-resolve — daily proposal archival.
#   * evolve.proposal_synthesizer — every-6h proposal-LLM synthesis.
#   * evolve.update-watcher — daily upstream-update checks.
#   * evolve.anthropic-admin-ingest — daily Anthropic cost-report snapshot.
#   * evolve.audit-scheduler — hourly infra audit + audit-poller drain.
#     (Renamed from evolve.app-test-scheduler on 2026-06-08 when the
#     app-test surface was killed; see internal/decision-app-tests-2026-06-08.md.)
#
# Per-bot backup jobs (``ai.evolve.<bot>.backup``) are intentionally NOT
# here — ``action.bot.backup_workspace`` is the per-bot path and it
# already exists. This tool is for pod-wide infra only.
_ALLOWED_INFRA_DAEMONS = frozenset({
    "evolve.heal",
    "evolve.verify",
    "evolve.repo-puller",
    "evolve.admin-ui",
    "evolve.signal-notifier",
    "evolve.pod-health",
    "evolve.audit",
    "evolve.weekly-review",
    "evolve.usage-logger",
    "evolve.retention",
    "evolve.cost_watchdog",
    "evolve.session_economics",
    "evolve.embedding_monitor",
    "evolve.proposal-auto-resolve",
    "evolve.proposal_synthesizer",
    "evolve.update-watcher",
    "evolve.anthropic-admin-ingest",
    "evolve.audit-scheduler",
})


def _daemon_plist_path(daemon_id: str) -> Path:
    """Where the daemon's launchd plist lives on disk. Validates by
    file existence — if the plist is missing, the daemon isn't
    installed on this pod (typical reason: ``evolve-admin
    install-infra-jobs`` was never run, or the install schema doesn't
    include this daemon)."""
    return Path(f"/Library/LaunchDaemons/ai.evolve.{daemon_id}.plist")


# ─── action.infra.daemon_restart ─────────────────────────────────────────────


def _daemon_restart_handler(
    daemon_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Kick an infra daemon via launchctl kickstart.

    Mechanism: ``sudo /bin/launchctl kickstart -k system/ai.evolve.<daemon_id>``.
    The ``-k`` ensures we restart even if the daemon is currently
    running (kills the running process, launchd starts a fresh one).

    Returns immediately after the kickstart; daemon restart is typically
    sub-second but downstream effects (e.g. heal completing its first
    sweep) take longer. Operator verifies indirectly via the chip
    clearing on the next tile-metrics refresh.

    Defense in depth: this handler validates the whitelist AND plist
    existence itself, not just at validate time. Even if a direct tool
    call bypasses validate, the handler refuses unknown ids.
    """
    if not daemon_id:
        return {"ok": False, "error": "daemon_id is required"}

    if daemon_id not in _ALLOWED_INFRA_DAEMONS:
        return {
            "ok": False,
            "error": (
                f"daemon_id '{daemon_id}' is not in the allowed infra "
                f"daemons list. This tool restarts pod-wide infra "
                f"daemons only (heal, verify, repo-puller, admin-ui, "
                f"signal-notifier, etc.). For per-bot backup, use "
                f"``action.bot.backup_workspace``. For per-bot gateway "
                f"restart, use ``action.bot.restart``."
            ),
        }

    # Phase E.3.4 path: try the admin daemon's protected endpoint first.
    # Falls back to in-process launchctl during the migration window.
    from ..admin_client import try_daemon_call
    used_daemon, status, body = try_daemon_call(
        "POST", f"/api/admin/infra/{daemon_id}/restart",
        body={"reason": reason or "evo restarted infra daemon via tool"},
    )
    if used_daemon and status == 200 and isinstance(body, dict):
        return {
            "ok": bool(body.get("ok", True)),
            "daemon_id": daemon_id,
            "launchd_label": f"ai.evolve.{daemon_id}",
            "reason": reason or "evo restarted infra daemon via tool",
            "via": "admin_daemon",
        }
    if used_daemon and status is not None:
        return {
            "ok": False,
            "error": f"admin daemon returned status {status}",
            "daemon_id": daemon_id,
            "daemon_body": body,
        }

    # Fallback: in-process launchctl kickstart (migration window only).
    plist = _daemon_plist_path(daemon_id)
    if not plist.exists():
        return {
            "ok": False,
            "error": (
                f"no launchd plist at {plist} — daemon '{daemon_id}' "
                f"is not installed on this pod. Run ``sudo evolve-admin "
                f"install-infra-jobs`` from the admin terminal to "
                f"install the canonical set."
            ),
        }

    # Kickstart via the Scheduler seam — same ``sudo /bin/launchctl
    # kickstart -k`` argv as before (privilege still comes from the
    # sudoers grant; the seam doesn't widen what this gateway
    # subprocess can do). The seam's runner never raises — timeouts/
    # OSErrors surface as (rc=1, message), so one branch covers both
    # the old exception path and the old non-zero-exit path.
    from ...runtime import get_scheduler
    ok, out = get_scheduler().restart(f"ai.evolve.{daemon_id}")
    if not ok:
        return {
            "ok": False,
            "error": f"launchctl kickstart failed: {out.strip() or 'no output'}",
            "daemon_id": daemon_id,
        }

    return {
        "ok": True,
        "daemon_id": daemon_id,
        "launchd_label": f"ai.evolve.{daemon_id}",
        "reason": reason or "evo restarted infra daemon via tool",
        # Post-action verify hint. The infra_daemon_down chip surfaces
        # on the admin tile (pod_state.bots projection) — when the
        # daemon comes back up, the chip clears on the next tile refresh
        # (~10s). For per-daemon liveness, pod_state.host doesn't help
        # directly (admin host stats only); pod_state.bots is the chip
        # carrier so we point there.
        "verify_via": {
            "tool": "pod_state.bots",
            "args": {},
            "expect": (
                "the infra_daemon_down chip on the admin (evolve) bot "
                "tile clears within ~10s; chip.detail no longer "
                f"lists '{daemon_id.split('.', 1)[-1]}' as missing"
            ),
        },
    }


def _daemon_restart_validate(
    daemon_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Dry-run gate: confirm the daemon id is in the whitelist AND its
    plist is installed. Both checks at validate time so the proxy can
    surface a clear reason in chat rather than rendering a button that
    errors at click time.

    Note: whitelist check is BEFORE plist check. A model that asks for
    a destructive non-whitelisted label gets the whitelist error, not
    a "plist missing" error that might be interpreted as "well let me
    install it".
    """
    if not daemon_id:
        return {"ok": False, "reason": "daemon_id is required"}

    if daemon_id not in _ALLOWED_INFRA_DAEMONS:
        # Don't list every allowed daemon in the error — the model can
        # query meta.tools or read the description for the full list.
        # Hint at the common ones so the operator gets a quick mental
        # match if they just typo'd.
        return {
            "ok": False,
            "reason": (
                f"daemon_id '{daemon_id}' is not in the allowed infra "
                f"daemons list. Common values include 'evolve.heal', "
                f"'evolve.verify', 'evolve.repo-puller', 'evolve.admin-ui', "
                f"'evolve.signal-notifier'. For per-bot operations, "
                f"use ``action.bot.restart`` (gateway) or "
                f"``action.bot.backup_workspace`` (backup job) instead."
            ),
        }

    plist = _daemon_plist_path(daemon_id)
    if not plist.exists():
        return {
            "ok": False,
            "reason": (
                f"daemon '{daemon_id}' is whitelisted but its launchd "
                f"plist isn't installed on this pod (no {plist}). Run "
                f"``sudo evolve-admin install-infra-jobs`` from the "
                f"admin terminal to install the canonical set, then "
                f"retry."
            ),
        }

    return {
        "ok": True,
        "context": {
            "daemon_id": daemon_id,
            "launchd_label": f"ai.evolve.{daemon_id}",
            "estimated_duration_s": 5,
        },
    }


DAEMON_RESTART_TOOL = Tool(
    name="action.infra.daemon_restart",
    description=(
        "Restart a pod-wide infra LaunchDaemon by kicking its launchd "
        "job (``sudo /bin/launchctl kickstart -k system/ai.evolve.<id>``). "
        "Use when an ``infra_daemon_down`` chip is firing on the admin "
        "tile, or when an operator says 'kick heal' / 'restart the "
        "puller' / 'verify is stuck'. The daemon_id is the suffix after "
        "``ai.evolve.`` in the launchd label — e.g. 'evolve.heal', "
        "'evolve.verify', 'evolve.repo-puller', 'evolve.admin-ui', "
        "'evolve.signal-notifier'. Only whitelisted infra daemons are "
        "accepted (defense in depth — the model can't accidentally "
        "kick arbitrary launchd jobs). For per-bot gateway restart use "
        "``action.bot.restart``; for per-bot backup use "
        "``action.bot.backup_workspace``. DO NOT propose a ``sudo "
        "launchctl kickstart`` shell snippet — this tool is the "
        "registered path and it's identical to what the operator "
        "would type."
    ),
    wire_description=(
        "Restart a whitelisted pod-wide infra LaunchDaemon (e.g. "
        "'evolve.heal', 'evolve.verify', 'evolve.repo-puller'). Use "
        "when an ``infra_daemon_down`` chip fires or the operator "
        "says 'kick heal'. Per-bot: use action.bot.restart (gateway) "
        "or action.bot.backup_workspace (backup). DO NOT propose a "
        "``sudo launchctl kickstart`` shell snippet — this tool is "
        "the registered path."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "daemon_id": {
                "type": "string",
                "description": (
                    "Infra daemon id (suffix after ``ai.evolve.`` in "
                    "the launchd label). Must be in the allowed set; "
                    "examples: 'evolve.heal', 'evolve.verify', "
                    "'evolve.repo-puller', 'evolve.admin-ui'."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "Optional reason for the audit log (e.g. 'chip "
                    "infra_daemon_down firing for heal' or 'operator "
                    "reports verify stuck since 14:00')."
                ),
            },
        },
        "required": ["daemon_id"],
        "additionalProperties": False,
    },
    handler=_daemon_restart_handler,
    risk_tier=RiskTier.WRITE_RISKY,
    validate=_daemon_restart_validate,
    tags=("action", "infra", "lifecycle"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(DAEMON_RESTART_TOOL)
