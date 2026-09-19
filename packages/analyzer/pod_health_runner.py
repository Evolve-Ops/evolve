#!/usr/bin/env python3
"""Pod-health Signal emitter for high-cadence (1-min) gateway liveness.

Runs only ``_check_gateways`` and emits/sweeps ``pod_health_gateways``
Signals so the alert notifier and Alerts UI see gateway transitions
promptly. Other ``pod_health`` categories (launchd state, file_security,
repo_ownership) are not in this fast path — they run only from the admin
UI Refresh button, the setup wizard, and ``evolve-admin doctor``.

Schedule: every 60s via ``ai.evolve.evolve.pod-health`` LaunchDaemon
(see ``_install_launchd_pod_health`` in ``packages/admin/evolve_admin/deploy.py``).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from evolve_admin.health import run_pod_health_signals_only

# Gateway state-machine layer — suppresses single-tick FAILs to filter
# out flaps. See packages/analyzer/gateway_state_machine.py for the
# rationale. Default policy: 2 consecutive FAILs (~2 min for 60s
# pod_health cadence) before surfacing.
import gateway_state_machine


def main(argv: list[str] | None = None) -> int:
    print(
        f"[pod-health] heartbeat ok @ {datetime.now(timezone.utc).isoformat()}",
        flush=True,
    )
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--network",
        default="/Users/Shared/evolve/network.json",
        help="Path to network.json (defaults to /Users/Shared/evolve/network.json).",
    )
    args = ap.parse_args(argv)
    network_path = Path(args.network)

    # Hand the state-machine filter to run_pod_health_signals_only so
    # gateway checks pass through it before _emit_health_signals fires
    # Signals. Other check categories are passed through unchanged.
    def _state_machine_filter(report, network):
        confirm = _confirm_failures_from_network(network)
        shared_dir = Path(network.get("sharedDir", "/Users/Shared/evolve"))
        report.checks, decision = gateway_state_machine.filter_gateway_checks(
            report.checks, shared_dir, confirm_failures=confirm,
        )
        if decision.suppressed_pending:
            print(
                f"[pod_health] gateway FAILs suppressed (pending confirmation): "
                f"{decision.suppressed_pending}",
                flush=True,
            )
        if decision.new_confirmations:
            print(
                f"[pod_health] gateway outage CONFIRMED: "
                f"{decision.new_confirmations}",
                flush=True,
            )
        if decision.recoveries:
            print(
                f"[pod_health] gateway recovered: {decision.recoveries}",
                flush=True,
            )

    run_pod_health_signals_only(
        network_path=network_path,
        pre_emit_filter=_state_machine_filter,
    )
    return 0


def _confirm_failures_from_network(network: dict) -> int:
    """Resolve ``alerts.gateway_state_machine.confirm_failures`` with
    a sane default; out-of-range values get clamped to [1, 10]."""
    alerts = network.get("alerts") if isinstance(network, dict) else None
    cfg = (alerts or {}).get("gateway_state_machine") if isinstance(alerts, dict) else None
    raw = (cfg or {}).get("confirm_failures") if isinstance(cfg, dict) else None
    try:
        n = int(raw) if raw is not None else gateway_state_machine.DEFAULT_CONFIRM_FAILURES
    except (TypeError, ValueError):
        n = gateway_state_machine.DEFAULT_CONFIRM_FAILURES
    return max(1, min(10, n))


if __name__ == "__main__":
    raise SystemExit(main())
