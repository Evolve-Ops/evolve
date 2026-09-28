#!/usr/bin/env python3
"""doctor_pass_runner.py — nightly per-bot `openclaw doctor --lint --json`.

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

Detection, not repair — and why (2026-09-14)
────────────────────────────────────────────
This job ran `openclaw doctor --fix` until 2026-09-14. Under OC 2026.9.2
that is a **pod-wide no-op**: every bot fails in ~2s with

    Doctor could not enter maintenance. Stop the Gateway through its
    service owner, then run openclaw doctor --fix. Error: Gateway service
    ownership or shutdown could not be verified.

Verified on all 9 bots of the reference pod from one 03:17 run. The gate is
explicit in OC's own source — the block that throws is wrapped in
``!isServiceRepairExternallyManaged() && await shouldManageGatewayService()``
— and the block it guards is the one that **stops the managed gateway before
repairing**. So the three ways out were: set
``OPENCLAW_SERVICE_REPAIR_POLICY=external`` and stop/start each gateway around
the run (correct per OC's model, but buys nightly downtime on every bot); set
it and let doctor rewrite config under a live gateway (what OC refuses to do
by default, and for good reason); or stop trying to repair on a timer.

This job takes the third. The nightly value was always **detection** — the
"repair" it nominally did was migrations and warnings, none of it a deploy
precondition (the one deploy-critical piece, clearing a stale plugin install
when its manifest schema changed, is `deploy._clear_stale_plugin_install` and
still runs in-line). Deliberate repair belongs to the upgrade path, which
already sets the policy and stops the daemon first
(`internal/dispatch/queued/oc-upgrade-is-a-guarded-change.md` §7a).

So: `openclaw doctor --lint --json` — read-only, no maintenance gate, exits 0,
and emits structured findings instead of a wall of prose.

Findings reach the operator, they don't just land in a log
─────────────────────────────────────────────────────────
The old job printed doctor's report to a log nobody reads. This one writes
the parsed result to

    {bot_home}/.openclaw/workspace/evolve/doctor-lint.json

which the bot user owns (this script runs AS the bot) and the `evolve` user
can read via the standing ACL. `doctor_lint_signal.emit_doctor_lint_signals`,
running pod-side as `evolve` under `pod_report`, turns those into Signals.
That bot→evolve hop is the same shape `bot_log_signal` uses via
`{shared_dir}/status/{bot}.json`; it exists because a bot user cannot write
the Signal store directly.

Usage:
    python3 doctor_pass_runner.py --bot-id <id>

The bot id is informational for the log line only — the script always runs
`openclaw` as the user it is itself invoked as, which the launchd plist sets
via UserName. It IS used to locate the artifact directory, via the invoking
user's own home, never by deriving a path from the bot id (bot id is not the
account name).

Exit code: 0 when the lint ran, whatever it found — findings are data, not a
runner failure, and a non-zero exit here would make launchd's
last-exit-status mean "this bot has findings", which is not a service
failure. Non-zero is reserved for "the lint could not be run at all"
(missing CLI, timeout, unparseable output).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from platform_profile import find_openclaw_cli

# Where the pod-side converter looks. Relative to the INVOKING user's home —
# never built from the bot id, which is not the account name (one bot lives on
# a differently-named account, and deriving the path would write into the
# wrong home or nowhere at all).
ARTIFACT_RELPATH = ".openclaw/workspace/evolve/doctor-lint.json"


def artifact_path(home: Path | None = None) -> Path:
    """Absolute path of the lint artifact for the invoking user."""
    return (home or Path.home()) / ARTIFACT_RELPATH


def write_artifact(payload: dict, *, home: Path | None = None) -> Path | None:
    """Atomically write the lint result. Returns the path, or None on failure.

    Best-effort by design: the artifact is how findings reach the operator,
    but a bot whose workspace is momentarily unwritable should still leave a
    complete record in its log rather than exiting non-zero and making
    launchd report a service failure for a disk hiccup.
    """
    path = artifact_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
        return path
    except Exception as exc:  # noqa: BLE001 — reported, never fatal
        print(f"[doctor-pass] could not write {path}: {exc}", flush=True)
        return None


def summarize(findings: list[dict]) -> dict[str, int]:
    """Count findings by severity, for the one-line log summary."""
    counts: dict[str, int] = {}
    for f in findings:
        sev = str(f.get("severity") or "unknown")
        counts[sev] = counts.get(sev, 0) + 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Nightly per-bot `openclaw doctor --lint --json`",
    )
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
        f"[doctor-pass] {args.bot_id}: starting `openclaw doctor --lint --json` "
        f"(timeout={args.timeout}s)",
        flush=True,
    )
    try:
        r = subprocess.run(
            # --lint --json: read-only findings as structured data. NOT --fix —
            # see the module docstring; under OC 2026.9.2 --fix cannot enter
            # maintenance from a timer and repairs nothing.
            # stderr is kept SEPARATE here (unlike the old --fix invocation,
            # which folded it into stdout for the prose log): doctor writes
            # progress chatter to stderr, and merging it corrupts the JSON on
            # stdout.
            [openclaw, "doctor", "--lint", "--json"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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

    try:
        report = json.loads(r.stdout)
        findings = list(report.get("findings") or [])
    except (json.JSONDecodeError, TypeError, AttributeError) as exc:
        # Unparseable output means the lint did not actually run — a real
        # runner failure, and the one case worth a non-zero exit. Dump what we
        # got (both streams) so the log says WHY rather than just "bad JSON".
        print(
            f"[doctor-pass] {args.bot_id}: could not parse `doctor --lint "
            f"--json` output after {elapsed:.1f}s (rc={r.returncode}): {exc}",
            flush=True,
        )
        if r.stdout:
            print(f"[doctor-pass] stdout: {r.stdout[:2000]}", flush=True)
        if r.stderr:
            print(f"[doctor-pass] stderr: {r.stderr[:2000]}", flush=True)
        return 1

    counts = summarize(findings)
    summary = ", ".join(f"{n} {sev}" for sev, n in sorted(counts.items())) or "none"
    print(
        f"[doctor-pass] {args.bot_id}: lint finished in {elapsed:.1f}s — "
        f"{len(findings)} finding(s) [{summary}], "
        f"{report.get('checksRun', '?')} checks run, "
        f"{report.get('checksSkipped', '?')} skipped",
        flush=True,
    )
    # Full findings stay in the log too: the artifact is for the Signal
    # converter, the log is for a human already reading this bot's logs.
    for f in findings:
        print(
            f"[doctor-pass]   [{f.get('severity', '?')}] "
            f"{f.get('checkId', '?')}: {f.get('message', '')}",
            flush=True,
        )

    written = write_artifact(
        {
            "bot_id": args.bot_id,
            "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "elapsed_seconds": round(elapsed, 1),
            "ok": bool(report.get("ok")),
            "checks_run": report.get("checksRun"),
            "checks_skipped": report.get("checksSkipped"),
            "findings": findings,
        },
    )
    if written:
        print(f"[doctor-pass] {args.bot_id}: wrote {written}", flush=True)

    # 0 whatever was found — see the module docstring on why findings are not
    # a service failure.
    return 0


if __name__ == "__main__":
    sys.exit(main())
