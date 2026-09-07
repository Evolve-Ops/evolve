"""Launchd entry point for the breakers audit-of-cause generator.

Usage (launchd):
    /Users/Shared/evolve-venv/bin/python3 run_breakers_audit.py \\
        --shared-dir /Users/Shared/evolve --once

Usage (manual):
    sudo -u evolve /Users/Shared/evolve-venv/bin/python3 \\
        /Users/Shared/evolve-repo/packages/analyzer/run_breakers_audit.py \\
        --shared-dir /Users/Shared/evolve --once
"""
import sys

from breakers.audit_generator import main

sys.exit(main(sys.argv[1:]))
