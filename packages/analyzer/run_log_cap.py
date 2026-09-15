"""Launchd entry point for the daily log-file size cap.

The analyzer package is installed in the runtime venv, so log_cap is
importable when this file is invoked directly by the venv interpreter
rather than via ``python3 -m log_cap``.

Usage (launchd):
    /Users/Shared/evolve-venv/bin/python3 run_log_cap.py

Usage (manual, ad-hoc paths):
    sudo -u evolve /Users/Shared/evolve-venv/bin/python3 \
        /Users/Shared/evolve-repo/packages/analyzer/run_log_cap.py \
        --max-bytes 10485760 --keep 3 \
        /Users/Shared/evolve/logs/audit.log
"""
import sys

from log_cap import _main

sys.exit(_main(sys.argv[1:]))
