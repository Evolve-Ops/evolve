"""Pod-state read tool — host.

Exposes ``pod_state.host`` — the admin host's CPU / memory / disk /
load / uptime. Thin wrapper over ``evolve_admin.host_health.collect_host_health``,
which is the same function the admin UI's HOST tile reads from.

Used when the operator asks "is the box healthy?", "is anything red?",
"how full is the disk?", or when evo is reasoning about whether a
running-out-of-disk signal corresponds to actual host pressure.

This tool projects host_health's full snapshot to just the headline
fields. The full snapshot (bytes counters, per-mount detail) is not
LLM-useful and just bloats prompts; a percentage + status tier per
resource is enough for reasoning.
"""

from __future__ import annotations

import logging
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


def _project_host(snap: dict[str, Any]) -> dict[str, Any]:
    """Pick the headline fields. Drops byte counters and per-mount detail.

    Status fields ('ok'/'warn'/'crit') come from host_health's own
    threshold logic — we surface them so the model can reason about
    severity without re-implementing the thresholds.
    """
    if not snap.get("available", True):
        # Degraded path — host_health returned because psutil isn't
        # installed. Pass the operator-facing reason through verbatim.
        return {
            "available": False,
            "hostname": snap.get("hostname"),
            "reason": snap.get("reason") or "host health unavailable",
        }

    mem = snap.get("memory") or {}
    disk = snap.get("disk") or {}
    load = snap.get("load_avg") or {}

    return {
        "available": True,
        "hostname": snap.get("hostname"),
        "cpu_percent": snap.get("cpu_percent"),
        "cpu_status": snap.get("cpu_status"),
        "memory_percent": mem.get("percent"),
        "memory_status": mem.get("status"),
        "disk_percent": disk.get("percent"),
        "disk_status": disk.get("status"),
        "disk_mount": disk.get("mount"),
        "load1": load.get("load1"),
        "load_status": snap.get("load_status"),
        "uptime_seconds": snap.get("uptime_seconds"),
    }


def _handler() -> dict[str, Any]:
    """Take a host-health snapshot. No filters — there's one host.

    Defensive against host_health import failure or runtime errors.
    The model can read 'available: false' and adjust its reply.
    """
    try:
        from evolve_admin.host_health import collect_host_health
    except ImportError as exc:
        log.warning("pod_state.host: host_health unavailable: %s", exc)
        return {
            "available": False,
            "reason": f"host_health module not importable: {exc}",
        }

    try:
        snap = collect_host_health()
    except Exception as exc:  # noqa: BLE001
        log.exception("pod_state.host: collect_host_health failed")
        return {
            "available": False,
            "reason": f"host_health failed: {exc}",
        }

    return _project_host(snap)


TOOL = Tool(
    name="pod_state.host",
    description=(
        "Return host (admin server) CPU / memory / disk / load / "
        "uptime. Each resource includes a percent and a status tier "
        "('ok' < ~75%, 'warn' ~75-90%, 'crit' >90% — exact thresholds "
        "vary per resource). When psutil isn't installed, returns "
        "{available: false, reason: ...} instead of partial data — "
        "the model should mention the limitation rather than guess. "
        "Use this for 'is the host healthy?', 'how full is the disk?', "
        "or when reasoning about whether a host_health signal "
        "corresponds to real pressure."
    ),
    input_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    handler=_handler,
    risk_tier=RiskTier.READ,
    tags=("pod_state", "host"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(TOOL)
