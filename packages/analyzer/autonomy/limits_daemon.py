"""autonomy.limits_daemon — the 5-minute limits + reflex evaluation pass.

Spec: internal/spec-autonomy-ladder-2026-06-10.md §1.3 + §3.3.

Installed as the ``ai.evolve.evolve.autonomy-limits`` LaunchDaemon
(evolve user, StartInterval 300 — ``install_evolve_infra_jobs``). The
permission monitor runs the identical evaluation on the audit cadence
as the slow backstop (the signal-subscriber-daemon precedent: fast
path + sweep safety net); both emit through
``permissions.monitor.emit_findings`` so signatures match and the
Signal store dedups.

Why its own daemon: the rung-3 daily cap is the per-integration
analogue of the ``daily_cap_usd`` breaker, and §3.3's rationale is
"during an active prompt-injection incident, minutes matter." The
audit pass is too slow to pause a runaway integration the same day it
blows its cap, or to step a probed integration down while the probing
is happening.

Each cycle, per bot: evaluate caps (pause/unpause + render), run the
demotion reflex, emit findings, then sweep-resolve ONLY the two types
this pass derives (``autonomy_limit_hit`` / ``autonomy_demoted``),
scoped to bots whose checks ran — a crashed check keeps its bot's
Signals firing (the AUTONOMY_SIGNAL_TYPES sweep rule). Streaks,
backfill, and drift stay on the monitor's cadence: none of them needs
minutes-grade latency.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any


_DAEMON_TYPES = frozenset({"autonomy_limit_hit", "autonomy_demoted"})


def run_pass(shared_dir: Path, config: dict[str, Any]) -> dict[str, Any]:
    """One evaluation pass across the pod. Returns a summary dict."""
    from evolve_config import get_members, get_primary

    from . import actions_ledger as _ledger
    from . import limits as _limits
    from . import reflex as _reflex

    bot_ids: list[str] = []
    primary = get_primary(config)
    if primary:
        bot_ids.append(primary)
    for member in get_members(config):
        if member and member not in bot_ids:
            bot_ids.append(member)

    findings: list[dict[str, Any]] = []
    swept_bots: set[str] = set()
    for bot_id in bot_ids:
        bot_findings: list[dict[str, Any]] = []
        ran_ok = True
        try:
            limit_findings, limits_ok = _limits.evaluate_bot(
                shared_dir, bot_id, config,
            )
            bot_findings.extend(limit_findings)
            ran_ok = ran_ok and limits_ok
        except Exception as exc:  # noqa: BLE001 — per-bot isolation
            ran_ok = False
            print(f"[autonomy/limits_daemon] {bot_id}: limits failed: {exc}",
                  file=sys.stderr)
        try:
            reflex_findings, reflex_ok = _reflex.run_bot(
                shared_dir, bot_id, config,
            )
            bot_findings.extend(reflex_findings)
            ran_ok = ran_ok and reflex_ok
        except Exception as exc:  # noqa: BLE001
            ran_ok = False
            print(f"[autonomy/limits_daemon] {bot_id}: reflex failed: {exc}",
                  file=sys.stderr)
        try:
            _ledger.prune(shared_dir, bot_id)
        except Exception as exc:  # noqa: BLE001 — retention is best-effort
            print(f"[autonomy/limits_daemon] {bot_id}: ledger prune failed: {exc}",
                  file=sys.stderr)
        for f in bot_findings:
            f["bot_id"] = bot_id
        findings.extend(bot_findings)
        if ran_ok:
            swept_bots.add(bot_id)

    swept_resolved = 0
    try:
        from permissions import monitor as _monitor
        from signals import store as _signals_store

        kept = _monitor.emit_findings(
            shared_dir,
            [f for f in findings if f["type"] in _DAEMON_TYPES],
        )
        if swept_bots:
            swept = _signals_store.sweep_resolve(
                shared_dir,
                producer=_monitor.PRODUCER,
                kept_signatures=kept,
                reason="auto-resolve: autonomy limit/demotion condition cleared",
                types=set(_DAEMON_TYPES),
                bot_ids=swept_bots,
            )
            swept_resolved = len(swept)
    except ImportError as exc:  # pragma: no cover — partial installs
        print(f"[autonomy/limits_daemon] signal store unavailable: {exc}",
              file=sys.stderr)

    return {
        "bots_checked": len(bot_ids),
        "findings": len(findings),
        "swept_resolved": swept_resolved,
    }


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="autonomy.limits_daemon",
        description=(
            "Evaluate rung-3 daily caps and the auto-demotion reflex for "
            "every pod bot; pause capped integrations and emit "
            "autonomy_limit_hit / autonomy_demoted Signals."
        ),
    )
    parser.add_argument(
        "--shared-dir", type=Path, default=None,
        help="Pod shared dir (default: resolved from network.json)",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single pass and exit (default).",
    )
    parser.add_argument(
        "--daemon", action="store_true",
        help="Run continuously, sleeping --interval-seconds between cycles.",
    )
    parser.add_argument(
        "--interval-seconds", type=int, default=300,
        help="Sleep between cycles in daemon mode (default 300 = 5 min).",
    )
    args = parser.parse_args(argv)

    from evolve_config import get_shared_dir, load_config
    from evolve_util import now_iso

    config = load_config()
    shared_dir = args.shared_dir or get_shared_dir(config)

    def _one_cycle() -> None:
        summary = run_pass(shared_dir, config)
        print(
            f"[autonomy/limits_daemon] {now_iso()} "
            f"bots={summary['bots_checked']} findings={summary['findings']} "
            f"swept={summary['swept_resolved']}",
            file=sys.stderr,
        )

    if args.daemon:
        import time
        while True:
            try:
                _one_cycle()
            except Exception as exc:  # noqa: BLE001
                print(f"[autonomy/limits_daemon] cycle error: {exc}", file=sys.stderr)
            time.sleep(args.interval_seconds)
    else:
        _one_cycle()
    return 0


__all__ = ["main", "run_pass"]
