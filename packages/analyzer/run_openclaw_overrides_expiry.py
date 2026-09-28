"""Launchd entry point for the daily openclaw-overrides expires_at enforcer.

Phase 5 of internal/spec-openclaw-json-derived-artifact-2026-05-24.md. Walks
{shared_dir}/sandbox/overrides/<bot>.json, deletes lapsed entries +
emits Signals (expired + pre_expiry).

Usage (launchd):
    /Users/Shared/evolve-venv/bin/python3 run_openclaw_overrides_expiry.py \
        --shared-dir /Users/Shared/evolve

Usage (manual):
    sudo -u evolve /Users/Shared/evolve-venv/bin/python3 \
        /Users/Shared/evolve-repo/packages/analyzer/run_openclaw_overrides_expiry.py \
        --shared-dir /Users/Shared/evolve
"""
import sys

from evolve_admin.openclaw_overrides_expiry import _main

sys.exit(_main(sys.argv[1:]))
