"""Tests for the signal_notifier wiring of forge_cost_guard.

Surfaced by the 2026-06-09 alert root-cause audit
(internal/alert-root-cause-audit-2026-06-09.md): a
``forge_session_message_cap_exceeded`` Signal had been firing with the
Alerts UI showing it, but the operator's chat stayed silent because
``forge_cost_guard`` was missing from the notifier allowlist. Same shape
as the 2026-06-03 pod_report outage and the 2026-05-20 cost_alerting
blackout — see the silent-monitor-allowlist-drift memory note.

forge_cost_guard's emit_signal helper calls signals.store.observe()
directly and never invokes dispatcher.send, so it is safe to route through
signal_notifier — distinct from cost_watchdog / spend_alert, which dispatch
themselves and belong on the deny-list.

Under the deny-list-by-default model (2026-06-09 inversion):

1. ``forge_cost_guard`` is NOT in the deny-list (``_DIRECT_DISPATCH_PRODUCERS``)
   — its Signals reach chat automatically.

2. ``_catalog_event_for_signal`` maps forge_cost_guard Signals to
   ``cost.forge_session_cap`` so the operator can mute forge cost events
   independently.

3. The deny-list is the single source of truth — schema.py mirrors it in
   ``alerts.signal_notifier.excluded_producers``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.alerts import signal_notifier as sn  # noqa: E402
from evolve_admin.config_sandbox import schema as cfg_schema  # noqa: E402


def test_forge_cost_guard_not_in_direct_dispatch_denylist() -> None:
    """forge_cost_guard uses signals.store.observe(), not dispatcher.send().
    It must NOT appear in the direct-dispatch deny-list — that list is only
    for producers that already send chat messages on their own."""
    assert "forge_cost_guard" not in sn._DIRECT_DISPATCH_PRODUCERS


def test_forge_cost_guard_catalog_event_mapping() -> None:
    """forge_cost_guard Signals must map to cost.forge_session_cap so the
    operator can mute forge cost events without losing other cost alerts."""
    sig = SimpleNamespace(producer="forge_cost_guard",
                          type="forge_session_message_cap_exceeded")
    assert sn._catalog_event_for_signal(sig) == "cost.forge_session_cap"


def test_direct_dispatch_producers_in_schema_excluded_producers() -> None:
    """The schema's excluded_producers stock_default must exactly match
    _DIRECT_DISPATCH_PRODUCERS — the single source of truth. Divergence
    means the UI presents a different default than the runtime uses."""
    entry = next(
        (k for k in cfg_schema.SCHEMA
         if k.path == "alerts.signal_notifier.excluded_producers"),
        None,
    )
    assert entry is not None, (
        "alerts.signal_notifier.excluded_producers tunable missing from schema"
    )
    assert set(entry.stock_default) == set(sn._DIRECT_DISPATCH_PRODUCERS), (
        "alerts.signal_notifier.excluded_producers stock_default in schema.py "
        "diverges from _DIRECT_DISPATCH_PRODUCERS in signal_notifier.py — these "
        "two lists must agree (see feedback_silent_monitor_allowlist_drift.md)."
    )
