"""schema.watchdog — WatchdogEvent (L6).

Spec §3.2. Records what the Evolve Watchdog observes. Stored as JSONL
at ``{shared_dir}/watchdog/{YYYY-MM-DD}.jsonl``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4


WatchdogEventType = Literal[
    # Meta-layer (Evolve Watchdog) — anomalies in how RSI itself is behaving.
    "proposal_volume_deviation",
    "auto_revert_rate_spike",
    "rejection_rate_spike",
    "verification_reliability_drop",
    "generator_dominance",
    "calibration_drift",
    "observation_extraction_drift",
    "meta_layer_cost_spike",
    # Operational (Sysadmin Watchdog) — gateway / platform / config alerts
    # that surface to the operator. These replace the old heal.py-emitted
    # "investigation" proposals: detection without a concrete fix is an
    # alert, not a proposal.
    "gateway_instability",
    "config_drift_unexplained",
    # "test_failure_pattern" retired 2026-06-08 — app-test surface killed
    # per docs/decision-app-tests-2026-06-08.md. Tag value preserved as a
    # comment so any legacy on-disk events still parse, but no producer
    # emits this any more.
]

WatchdogSeverity = Literal["info", "warn", "alert"]


@dataclass
class WatchdogEvent:
    id: str
    bot_id: str | None  # None for pod-wide observations
    timestamp: str  # ISO8601 UTC
    event_type: WatchdogEventType
    severity: WatchdogSeverity
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "bot_id": self.bot_id,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "severity": self.severity,
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WatchdogEvent":
        return cls(
            id=data["id"],
            bot_id=data.get("bot_id"),
            timestamp=data["timestamp"],
            event_type=data["event_type"],
            severity=data["severity"],
            details=dict(data.get("details") or {}),
        )


def new_watchdog_event_id() -> str:
    return str(uuid4())
