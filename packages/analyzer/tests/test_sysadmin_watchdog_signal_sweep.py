"""tests/test_sysadmin_watchdog_signal_sweep.py — sweep-resolve behavior.

When a Sysadmin Watchdog detector goes silent (the underlying condition
clears), the runner's signal-sweep pass should auto-resolve the
corresponding firing Signal so the alert disappears from the Maintenance
lane without operator action. This test exercises that loop end-to-end
for the gateway-down detector.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from generators.sysadmin_watchdog import observe_signals  # noqa: E402
from generators.sysadmin_watchdog.observe import DetectorContext  # noqa: E402
from metrics.registry import MetricValue  # noqa: E402
from signals import store as signals_store  # noqa: E402


_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _resolver(metric_values: dict[str, MetricValue]) -> Callable:
    def r(name, bot_id, t):
        return metric_values.get(name, MetricValue(value=1.0, confidence=1.0))

    return r


def _ctx(metric_values: dict[str, MetricValue], shared_dir: Path) -> DetectorContext:
    return DetectorContext(
        bot_id="team_bot_a",
        now=_NOW,
        resolve=_resolver(metric_values),
        shared_dir=shared_dir,
    )


def _run_one_cycle(ctx: DetectorContext, shared_dir: Path) -> set[str]:
    """Mirror the generator runner's per-cycle Signal flow.

    Returns the set of signatures kept this cycle (used by the sweep).
    """
    kept: set[str] = set()
    for spec in observe_signals(ctx):
        signals_store.observe(shared_dir, **spec)
        kept.add(spec["signature"])
    return kept


def _sweep(producer: str, bot_id: str, kept: set[str], shared_dir: Path) -> None:
    """Mirror generator_runner.run_generators' signal-sweep pass."""
    for sig in list(signals_store.iter_active(shared_dir, producer=producer)):
        if sig.bot_id != bot_id:
            continue
        if sig.signature in kept:
            continue
        signals_store.apply_transition(
            sig,
            "resolved",
            shared_dir,
            actor="generator_runner",
            reason="detector silent: condition cleared",
        )


def test_gateway_signal_auto_resolves_when_detector_goes_silent(tmp_path):
    # Cycle 1: gateway is down past the chronic threshold → signal fires at "alert" severity
    down_ctx = _ctx(
        {
            "gateway.up": MetricValue(0.0),
            "gateway.consecutive_failures_24h": MetricValue(10.0),
        },
        tmp_path,
    )
    kept = _run_one_cycle(down_ctx, tmp_path)
    assert kept, "expected gateway_down signal to fire on cycle 1"

    actives_after_cycle1 = list(
        signals_store.iter_active(tmp_path, producer="sysadmin_watchdog")
    )
    assert len(actives_after_cycle1) == 1
    assert actives_after_cycle1[0].type == "gateway_down"
    assert actives_after_cycle1[0].state == "firing"
    assert actives_after_cycle1[0].severity == "alert"

    # Cycle 2: gateway recovered → no signal emitted; sweep should archive the firing one
    up_ctx = _ctx({"gateway.up": MetricValue(1.0)}, tmp_path)
    kept2 = _run_one_cycle(up_ctx, tmp_path)
    assert kept2 == set()
    _sweep("sysadmin_watchdog", "team_bot_a", kept2, tmp_path)

    actives_after_cycle2 = list(
        signals_store.iter_active(tmp_path, producer="sysadmin_watchdog")
    )
    assert actives_after_cycle2 == []


def test_gateway_signal_persists_when_still_firing(tmp_path):
    """Sweep must not resolve a Signal that the detector re-emitted this cycle."""
    down_ctx = _ctx(
        {
            "gateway.up": MetricValue(0.0),
            "gateway.consecutive_failures_24h": MetricValue(3.0),
        },
        tmp_path,
    )
    _run_one_cycle(down_ctx, tmp_path)

    # Same conditions next cycle — the signal continues to fire and the
    # observation count bumps.
    kept2 = _run_one_cycle(down_ctx, tmp_path)
    _sweep("sysadmin_watchdog", "team_bot_a", kept2, tmp_path)

    actives = list(signals_store.iter_active(tmp_path, producer="sysadmin_watchdog"))
    assert len(actives) == 1
    assert actives[0].state == "firing"
    assert actives[0].observation_count == 2
