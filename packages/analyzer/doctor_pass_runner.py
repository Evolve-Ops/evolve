#!/usr/bin/env python3
"""doctor_pass_runner.py — nightly per-bot `openclaw doctor --fix`.

The deploy path used to invoke `openclaw doctor --fix` synchronously
before `plugins install`. That worked for a long time, but on the
2026-05-29/30 `deploy --all` runs doctor started hitting 60s+ timeouts
on 6 of 8 bots — a hang that only manifested inside deploy.py's
subprocess wrapper and that I could never reproduce manually under
the same exact invocation (manual runs consistently completed in
12-15s, even as the same `evolve` user with the same flags). Rather
than keep chasing the discrepancy and burning deploy time, doctor
moved to a nightly launchd job:

    ai.openclaw.evolve.doctor-pass.<bot_id>

Each per-bot job runs this script as the bot's macOS user, which then
shells out to `openclaw doctor --fix`. Doctor's work (model-ref
migrations, cron-payload upgrades, orphan-transcript reports, security
warnings) is maintenance, not a deploy precondition — the one
deploy-critical piece (clearing a stale plugin install when its
manifest schema changed) is handled by `deploy._clear_stale_plugin_install`
which still runs in-line.

Usage:
    python3 doctor_pass_runner.py --bot-id <id>

The bot id is informational only — the script always runs `openclaw`
as the user the script itself is invoked as, which the launchd plist
sets via UserName. Logs to stdout (captured to
{bot_home}/.openclaw/logs/evolve-doctor-pass.log via the plist).

Exit code reflects doctor's exit code so launchd's last-exit-status
can surface failures.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time

OPENCLAW = "/opt/homebrew/bin/openclaw"


def main() -> int:
    parser = argparse.ArgumentParser(description="Nightly per-bot doctor --fix")
    parser.add_argument(
        "--bot-id",
        required=True,
        help="Bot ID — for log readability only; the script runs as whatever "
        "macOS user the launchd plist sets it to.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Max seconds to wait for doctor to complete (default: 600). "
        "Generous on purpose — nightly runs aren't on a critical path, "
        "and doctor on a stale-state bot can take a few minutes.",
    )
    args = parser.parse_args()

    if not shutil.which(OPENCLAW) and not _exists(OPENCLAW):
        print(f"[doctor-pass] {OPENCLAW} not found — skipping", flush=True)
        return 0

    started = time.monotonic()
    print(
        f"[doctor-pass] {args.bot_id}: starting `openclaw doctor --fix` "
        f"(timeout={args.timeout}s)",
        flush=True,
    )
    try:
        r = subprocess.run(
            [OPENCLAW, "doctor", "--fix"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=args.timeout,
        )
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - started
        print(
            f"[doctor-pass] {args.bot_id}: TIMEOUT after {elapsed:.1f}s "
            f"(limit {args.timeout}s); next run tomorrow",
            flush=True,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 — log anything else and exit non-zero
        elapsed = time.monotonic() - started
        print(
            f"[doctor-pass] {args.bot_id}: raised after {elapsed:.1f}s: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        return 1

    elapsed = time.monotonic() - started
    # Forward the captured output verbatim so the log has the full doctor
    # report (model upgrades, warnings, etc.) for the operator.
    if r.stdout:
        print(r.stdout, flush=True)
    print(
        f"[doctor-pass] {args.bot_id}: doctor finished in {elapsed:.1f}s "
        f"(rc={r.returncode})",
        flush=True,
    )
    return r.returncode


def _exists(path: str) -> bool:
    import os
    return os.path.exists(path)


if __name__ == "__main__":
    sys.exit(main())
