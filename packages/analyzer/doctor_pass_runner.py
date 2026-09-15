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
can surface failures. A missing openclaw CLI is itself an exit-1
failure: this job's only purpose is to run that CLI, so "not
installed" is a real misconfiguration the service manager should
surface, not something to skip quietly.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time

from platform_profile import find_openclaw_cli


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

    # Resolved at CALL time, never at import: the binary lives at a different
    # absolute path per platform (macOS Homebrew symlink vs Linux
    # /usr/bin/openclaw vs the node_modules entrypoints), and a module-level
    # constant both hardcodes the macOS one — every Linux-pod run of this job
    # failed on `/opt/homebrew/bin/openclaw not found` — and defeats tests
    # that pin the platform profile.
    openclaw = find_openclaw_cli()
    if openclaw is None:
        print(
            f"[doctor-pass] {args.bot_id}: openclaw CLI not found on PATH or "
            "at any known install location — cannot run doctor",
            flush=True,
        )
        return 1

    started = time.monotonic()
    print(
        f"[doctor-pass] {args.bot_id}: starting `openclaw doctor --fix` "
        f"(timeout={args.timeout}s)",
        flush=True,
    )
    try:
        r = subprocess.run(
            [openclaw, "doctor", "--fix"],
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


if __name__ == "__main__":
    sys.exit(main())
