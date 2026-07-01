"""Launchd entry point for the circuit-breakers detector runner.

Drives ``breakers.runner`` on the pod's recent turn data every cycle. The
runner is observe-only by default: it always evaluates the activity-shape
detector and appends a structured entry to ``{shared_dir}/breakers/runner-log/``
("what would have tripped"), but only acts on trips when
``network.json::breakers.auto_trip_enabled`` is true (default ``false``). This
daemon starts the §5.2 calibration soak — it does NOT arm enforcement.

Usage (launchd):
    /Users/Shared/evolve-venv/bin/python3 run_breakers_runner.py \\
        --shared-dir /Users/Shared/evolve --once

Usage (manual):
    sudo -u evolve /Users/Shared/evolve-venv/bin/python3 \\
        /Users/Shared/evolve-repo/packages/analyzer/run_breakers_runner.py \\
        --shared-dir /Users/Shared/evolve --once

Spec: docs/spec-circuit-breakers-2026-05-21.md §5.1 / §8 Phase 5.
"""
import sys

from breakers.runner import main

sys.exit(main(sys.argv[1:]))
