"""Launchd entry point for the daily Anthropic Admin API ingest.

The analyzer and ``evolve_admin`` packages are installed in the
runtime venv, so all imports resolve normally.

Usage (launchd):
    /Users/Shared/evolve-venv/bin/python3 run_anthropic_admin_ingest.py \\
        --shared-dir /Users/Shared/evolve \\
        --network /Users/Shared/evolve/network.json

Usage (manual):
    sudo -u evolve /Users/Shared/evolve-venv/bin/python3 \\
        /Users/Shared/evolve-repo/packages/analyzer/run_anthropic_admin_ingest.py \\
        --shared-dir /Users/Shared/evolve
"""
import sys

from evolve_admin.anthropic_admin_ingest import _main

sys.exit(_main(sys.argv[1:]))
