"""Launchd entry point for the daily OC gateway-log rotator.

See ``evolve_admin.oc_log_rotate`` for the design notes.

Usage (launchd):
    /Users/Shared/evolve-venv/bin/python3 run_oc_log_rotate.py

Usage (manual / dry-run):
    sudo -u evolve /Users/Shared/evolve-venv/bin/python3 \\
        /Users/Shared/evolve-repo/packages/analyzer/run_oc_log_rotate.py --dry-run
"""
import sys

from evolve_admin.oc_log_rotate import _main

sys.exit(_main(sys.argv[1:]))
