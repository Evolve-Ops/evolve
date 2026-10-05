"""Launchd entry point for the daily proposal auto-resolve sweep.

Usage (launchd):
    /Users/Shared/evolve-venv/bin/python3 run_proposal_auto_resolve.py \\
        --shared-dir /Users/Shared/evolve

Usage (manual):
    sudo -u evolve /Users/Shared/evolve-venv/bin/python3 \\
        /Users/Shared/evolve-repo/packages/analyzer/run_proposal_auto_resolve.py \\
        --shared-dir /Users/Shared/evolve
"""
import sys

from arbiter.auto_resolve import _main

sys.exit(_main(sys.argv[1:]))
