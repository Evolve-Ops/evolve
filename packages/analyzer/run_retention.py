"""Launchd entry point for the daily data-retention pruner.

Usage (launchd):
    /Users/Shared/evolve-venv/bin/python3 run_retention.py \
        --shared-dir /Users/Shared/evolve

Usage (manual):
    sudo -u evolve /Users/Shared/evolve-venv/bin/python3 \
        /Users/Shared/evolve-repo/packages/analyzer/run_retention.py \
        --shared-dir /Users/Shared/evolve
"""
import sys

from signals.retention import _main

sys.exit(_main(sys.argv[1:]))
