"""gateway_state_machine — flap suppression for gateway-down detection.

Security_bot-style state machine for pod_health_gateways signals. The
current pod_health_runner emits a Signal on every single failed
probe, which means a gateway that flaps (down → up → down → up...)
produces signal-state churn even when signal_notifier's generic
cooldown prevents alert spam. Security_bot's gateway-state.json model
required N consecutive failures before declaring DOWN, and emitted
recovery immediately on the first PASS after a real outage.

This module is a pure state-machine layer applied between
``_check_gateways`` and ``_emit_health_signals``. Persistent state
lives at ``{shared_dir}/state/gateway-state.json`` and tracks
per-gateway consecutive-failure counts. The 60-second pod_health
cron is unchanged; the state machine just decides which FAIL checks
to actually surface this tick.

Default policy:
  - FAIL emits only after 2 consecutive failures (~2 minutes of
    confirmed downtime; one transient blip is suppressed).
  - PASS after a confirmed-DOWN state resets consecutive_failures
    and lets sweep_resolve clear the firing signal — the operator
    sees the recovery on the next tick.
  - The 2-tick suppression is the floor; configurable per pod via
    ``network.json::alerts.gateway_state_machine.confirm_failures``.

Atomic writes via temp + os.replace; persistent state owned by the
evolve user under ``{shared_dir}/state/`` (auto-created if missing).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

STATE_SCHEMA_VERSION = 1
DEFAULT_CONFIRM_FAILURES = 2     # 2 ticks = ~2 min for 60s pod-health cadence
GATEWAY_CHECK_CATEGORY = "gateways"


@dataclass
class GatewayProbe:
    """One gateway check result. Mirror of the fields ``_check_gateways``
    writes onto ``HealthReport.checks`` — using a plain dataclass keeps
    this module independent of the admin package."""
    name: str             # e.g. "admin_bot:gateway"
    status: str           # "PASS" | "FAIL" | "WARN"
    detail: str = ""


@dataclass
class GatewayState:
    """Per-gateway persistent state."""
    name: str
    consecutive_failures: int = 0
    confirmed_down: bool = False     # True once consecutive_failures hit confirm threshold
    last_status: str = ""             # "PASS" | "FAIL" | "WARN"
    last_changed_at: float = 0.0     # epoch seconds


@dataclass
class StateMachineDecision:
    """Result for a single tick.

    ``surface`` is the subset of input probes that should actually be
    surfaced (i.e. emitted as Signals) this tick. FAIL probes whose
    consecutive count is below the confirmation threshold are filtered
    out — the state machine remembers them internally and surfaces them
    once they confirm.
    """
    surface: list[GatewayProbe] = field(default_factory=list)
    suppressed_pending: list[str] = field(default_factory=list)  # names of FAILs below threshold
    new_confirmations: list[str] = field(default_factory=list)    # gateways that just crossed threshold this tick
    recoveries: list[str] = field(default_factory=list)            # gateways that flipped DOWN→UP this tick


def _state_path(shared_dir: Path) -> Path:
    return shared_dir / "state" / "gateway-state.json"


def load_state(shared_dir: Path) -> dict[str, GatewayState]:
    """Read persistent state. Returns {} when missing or corrupt."""
    p = _state_path(shared_dir)
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    gateways = data.get("gateways")
    if not isinstance(gateways, dict):
        return {}
    out: dict[str, GatewayState] = {}
    for name, entry in gateways.items():
        if not isinstance(entry, dict):
            continue
        try:
            out[name] = GatewayState(
                name=name,
                consecutive_failures=int(entry.get("consecutive_failures") or 0),
                confirmed_down=bool(entry.get("confirmed_down") or False),
                last_status=str(entry.get("last_status") or ""),
                last_changed_at=float(entry.get("last_changed_at") or 0.0),
            )
        except (TypeError, ValueError):
            continue
    return out


def save_state(shared_dir: Path, state: dict[str, GatewayState]) -> None:
    """Atomic write via temp + os.replace."""
    p = _state_path(shared_dir)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    payload = {
        "schema_version": STATE_SCHEMA_VERSION,
        "updated_at": time.time(),
        "gateways": {
            g.name: {
                "consecutive_failures": g.consecutive_failures,
                "confirmed_down": g.confirmed_down,
                "last_status": g.last_status,
                "last_changed_at": g.last_changed_at,
            } for g in state.values()
        },
    }
    tmp = p.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, p)
    except OSError:
        pass


def apply_state_machine(
    probes: Iterable[GatewayProbe],
    state: dict[str, GatewayState],
    *,
    confirm_failures: int = DEFAULT_CONFIRM_FAILURES,
    now: float | None = None,
) -> StateMachineDecision:
    """Pure function — decide which probes to surface this tick + mutate state.

    Mutates the input ``state`` dict in place. Returns the decision.

    Rules:
      - PASS resets consecutive_failures + clears confirmed_down. Surfaced
        as PASS so the existing sweep_resolve clears any firing Signal.
      - FAIL increments consecutive_failures.
        - When count >= confirm_failures and gateway was not previously
          confirmed_down, mark confirmed_down + surface as FAIL + add to
          new_confirmations (security_bot-style "this just became real").
        - When already confirmed_down, surface as FAIL (continued outage —
          let signal-store's find-or-create dedup handle Signal lifecycle).
        - When below threshold, suppress: don't surface; record name in
          suppressed_pending so caller can log.
      - WARN passes through unchanged (state machine doesn't change WARN
        behavior — only FAIL needs confirmation).
    """
    if now is None:
        now = time.time()
    decision = StateMachineDecision()

    for probe in probes:
        prior = state.get(probe.name) or GatewayState(name=probe.name)
        if probe.status == "PASS":
            was_confirmed_down = prior.confirmed_down
            prior.consecutive_failures = 0
            prior.confirmed_down = False
            if prior.last_status != "PASS":
                prior.last_changed_at = now
            prior.last_status = "PASS"
            state[probe.name] = prior
            decision.surface.append(probe)
            if was_confirmed_down:
                decision.recoveries.append(probe.name)
        elif probe.status == "FAIL":
            prior.consecutive_failures += 1
            if prior.last_status != "FAIL":
                prior.last_changed_at = now
            prior.last_status = "FAIL"
            if prior.consecutive_failures >= confirm_failures:
                if not prior.confirmed_down:
                    decision.new_confirmations.append(probe.name)
                prior.confirmed_down = True
                decision.surface.append(probe)
            else:
                decision.suppressed_pending.append(probe.name)
            state[probe.name] = prior
        else:
            # WARN or unknown status — pass through unchanged.
            decision.surface.append(probe)
            prior.last_status = probe.status
            state[probe.name] = prior

    return decision


def filter_gateway_checks(
    checks: list,
    shared_dir: Path,
    *,
    confirm_failures: int = DEFAULT_CONFIRM_FAILURES,
    now: float | None = None,
) -> tuple[list, StateMachineDecision]:
    """Wrapper for ``HealthReport.checks``-shaped lists.

    Walks ``checks`` (expected to have ``.status``, ``.category``,
    ``.name``, optional ``.detail`` attributes); applies the state
    machine to entries with ``category == "gateways"``; passes other
    checks through unchanged. Returns ``(filtered_checks, decision)``.

    Persistent state is read at the start and written at the end —
    one round-trip per pod_health tick.
    """
    state = load_state(shared_dir)
    gateway_probes = []
    gateway_indices: dict[str, int] = {}
    out: list = list(checks)
    for i, c in enumerate(out):
        if getattr(c, "category", None) == GATEWAY_CHECK_CATEGORY:
            gateway_probes.append(GatewayProbe(
                name=c.name, status=c.status, detail=getattr(c, "detail", ""),
            ))
            gateway_indices[c.name] = i

    decision = apply_state_machine(
        gateway_probes, state, confirm_failures=confirm_failures, now=now,
    )

    # Filter out suppressed checks. Iterate in reverse so index removal
    # doesn't shift unprocessed indices.
    suppressed_set = set(decision.suppressed_pending)
    for i in range(len(out) - 1, -1, -1):
        c = out[i]
        if (
            getattr(c, "category", None) == GATEWAY_CHECK_CATEGORY
            and c.name in suppressed_set
        ):
            del out[i]

    save_state(shared_dir, state)
    return out, decision
