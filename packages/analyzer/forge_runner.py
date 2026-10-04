#!/usr/bin/env python3
# evolve: pkg=p-00000000@2026.04.12-1.0 file=f-00000001@2026.04.12.1
"""
forge_runner.py — Bot-side entry point for Forge engine jobs.

This script is invoked by a bot session (typically via a defer-fired agent
run) when a forge job needs execution. It bridges the bot's execution
context (packages/analyzer/) to the forge engine in the admin package
(packages/admin/evolve_admin/applications/).

Usage (called from a bot session — directly or via the defer plugin tool):
    python3 forge_runner.py --job-id j-7bc21e44 --bot-id admin_bot

The script:
1. Resolves shared_dir from evolve_config
2. Calls forge_engine.run_forge_job(job_id, shared_dir, bot_id)
3. Exits 0 on success, 1 on failure

All detailed logging and job state management is handled by forge_engine.py
and forge_jobs.py — this script is intentionally thin.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# This script lives at packages/analyzer/forge_runner.py.
# The admin package is at packages/admin/ — two levels up then down.

_THIS_DIR = Path(__file__).resolve().parent          # packages/analyzer/
_REPO_ROOT = _THIS_DIR.parent                        # packages/
_ADMIN_PKG = _REPO_ROOT / "admin"                   # packages/admin/


# ── Imports ───────────────────────────────────────────────────────────────────

def _import_forge_engine():
    """Import forge_engine, handling both installed-package and direct-path cases."""
    try:
        # Try installed-package import first (evolve_admin is installed in the venv)
        from evolve_admin.applications import forge_engine
        return forge_engine
    except ImportError:
        pass
    # Fallback: direct path import
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "forge_engine",
        _ADMIN_PKG / "evolve_admin" / "applications" / "forge_engine.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _get_shared_dir() -> Path:
    """Resolve shared_dir from evolve_config (same as other analyzer scripts).

    W10-G #3: ``get_shared_dir`` requires the loaded config — a no-arg call
    raised TypeError, which the broad ``except`` swallowed, so this ALWAYS
    fell through to the hardcoded fallback (ignoring a non-default
    ``sharedDir`` and breaking on Linux). Load config + pass it; the fallback
    is now platform-keyed.
    """
    try:
        from evolve_config import get_shared_dir, load_config
        return Path(get_shared_dir(load_config()))
    except Exception:
        # Hard fallback to the platform-keyed canonical location.
        from platform_profile import get_profile
        return Path(get_profile().shared_dir_default)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Run a forge engine job")
    parser.add_argument("--job-id", required=True, help="Forge job ID (j- prefixed)")
    parser.add_argument("--bot-id", required=True, help="Bot ID executing this job")
    parser.add_argument(
        "--shared-dir",
        default=None,
        help="Path to shared evolve directory (default: from evolve_config)",
    )
    args = parser.parse_args()

    shared_dir = Path(args.shared_dir) if args.shared_dir else _get_shared_dir()

    print(f"[forge_runner] Starting job {args.job_id} for bot {args.bot_id}")
    print(f"[forge_runner] shared_dir: {shared_dir}")

    try:
        forge_engine = _import_forge_engine()
    except Exception as e:
        print(f"[forge_runner] ERROR: could not import forge_engine: {e}", file=sys.stderr)
        return 1

    try:
        forge_engine.run_forge_job(
            job_id=args.job_id,
            shared_dir=shared_dir,
            bot_id=args.bot_id,
        )
        print(f"[forge_runner] Job {args.job_id} completed (awaiting operator approval)")
        return 0
    except Exception as e:
        print(f"[forge_runner] ERROR in forge job {args.job_id}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
