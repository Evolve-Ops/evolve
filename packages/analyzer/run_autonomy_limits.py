"""Launchd entry point for the autonomy limits + demotion-reflex pass.

Usage (launchd — ai.evolve.evolve.autonomy-limits, every 5 min):
    /Users/Shared/evolve-venv/bin/python3 run_autonomy_limits.py \\
        --shared-dir /Users/Shared/evolve --once

Usage (manual):
    sudo -u evolve /Users/Shared/evolve-venv/bin/python3 \\
        /Users/Shared/evolve-repo/packages/analyzer/run_autonomy_limits.py \\
        --shared-dir /Users/Shared/evolve --once
"""
import sys

from autonomy.limits_daemon import main

sys.exit(main(sys.argv[1:]))
