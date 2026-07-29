"""schema.signal — Signal v1 dataclasses.

Spec: docs/spec-alerts-signal-store-2026-05-07.md §4.

A Signal is the unit of observation written by *monitors* (pod_report,
watchdog, audit, pod_health, host_health, integration_probe,
error_reporter, etc.). Distinct from a Proposal, which is the unit of
*action* written by generators.

Every Signal carries:
  - Identity (id, signature for find-or-create dedup)
  - Origin (producer, type, flavor, severity, scope, bot_id)
  - Body (title, body, details payload)
  - Lifecycle (state, observation counters, snooze, history)
  - Cross-links (motivated_proposals — denormalized; canonical link
    lives on Proposal.motivating_signals)
  - Delivery (audit log of dispatch attempts)

State transitions are authoritative on the JSON; the storage subdir is
a physical index for efficient iteration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

from evolve_util import now_iso_offset as _utc_now_iso


# ─────────────────────────────────────────────────────────────────────────────
# Type aliases
# ─────────────────────────────────────────────────────────────────────────────

Flavor = Literal["activity", "maintenance"]
Severity = Literal["info", "warn", "alert"]
Scope = Literal["pod", "bot", "host", "integration"]

State = Literal["firing", "snoozed", "resolved", "dismissed"]

# High-level domain bucket — the Alerts page uses this for top-level
# tabbing/filtering so 33 producers don't all sit in one flat list.
# Producer remains open-ended (string), but every emission gets routed
# into exactly one category. Unknown producers default to "platform".
Category = Literal[
    "security", "cost", "platform", "integrations", "backup", "hygiene"
]

# Producer → category routing. Mirrors the recommendation from the
# 2026-06-03 Alerts review. Stored separately from the dataclass so a
# future per-signal override can layer on top without touching the map.
PRODUCER_CATEGORY_DEFAULT: dict[str, Category] = {
    # Security & compliance
    "security_warden": "security",
    "compliance_scan": "security",
    "plugin_monitor": "security",
    "content_scan": "security",
    "mcp_monitor": "security",
    "permission_monitor": "security",
    # Out-of-band changes to a bot's group allowlist (who can access/spend the
    # bot's tokens in a channel) vs the admin-recorded baseline — a least-
    # privilege governance concern. R1a PR3; detection-only.
    "roster_allowlist_drift": "security",
    # Cost & efficiency
    "cost_watchdog": "cost",
    "session_economics": "cost",
    "cascade_audit": "cost",
    "spend_alert": "cost",
    "embedding_monitor": "cost",
    # Backups
    "backup_signal": "backup",
    "backup_audit_signal": "backup",
    "local_backup_signal": "backup",
    # Integrations
    "integration_probe": "integrations",
    # Platform health & install integrity (catch-all)
    "registry": "platform",
    "monitor_coverage": "platform",
    "deploy_drift_monitor": "platform",
    "install_integrity_monitor": "platform",
    "sysadmin_watchdog": "platform",
    "pod_report": "platform",
    "audit": "platform",
    "breakers_runner": "platform",
    "host_health": "platform",
    "heal": "platform",
    "openclaw_config_validator": "platform",
    "error_reporter": "platform",
    "bot_recovery_monitor": "platform",
    "upstream_issues_watcher": "platform",
    # OpenClaw surface drift-diff from update_watcher (CLI/extensions/channels/
    # config-schema changed in a candidate OC release). Platform-health adjacent.
    "oc_surface_drift": "platform",
    "repo_puller": "platform",
    # Managed calendar jobs whose last launchd run exited non-zero
    # (PR #2669 follow-up) — install/maintenance integrity.
    "cron_exit_monitor": "platform",
    # Proactive-delivery monitor (U2.1) — per-window delivery outcomes for
    # scheduled user-facing apps. "platform" per spec §7; an "apps"
    # category is the spec's Open Question 6, deferred.
    "delivery_monitor": "platform",
    # Post-OC-upgrade send-surface verification — fires only when an
    # upgrade broke the message-send path (the 2026-06-11 P0 shape).
    "send_surface_probe": "platform",
    # Black-box end-to-end probe of the "evo" keyword path
    # (gateway plugin → admin /api/evo/dispatch). Fires only when that
    # pod-wide path is broken (device-auth 401, daemon down, socket
    # path/perm break — the #3257/#3160/#3223 shapes). Sibling to
    # send_surface_probe; a platform-integrity probe.
    "evo_path_probe": "platform",
    # Agent-invoked app scripts that fail at runtime (the "(agent) failed"
    # exec chip). Sibling to the agent-bypass audit; an app-behavior /
    # platform-integrity finding, so "platform" (the apps-shaped catch-all,
    # same as app_structural_verifier and delivery_monitor).
    "app_script_failure_audit": "platform",
    # Forge apply-time outcomes — scheduled-action materialize failures
    # (audit slate S2) + install cost overruns. Install-integrity shaped;
    # "platform" (the apps-shaped catch-all, same as delivery_monitor).
    "forge_engine": "platform",
    # Advisory / cleanup-y advisories
    "proposal_synthesizer": "hygiene",
    # Model discovery — "a newer / unknown model line is available; consider
    # adopting it into a rung". Substrate-hygiene advisory.
    "model_discovery": "hygiene",
    # Per-bot utilization baseline (bot_underused + coverage). Advisory
    # bucket — least-wrong existing category; a dedicated "value" category
    # waits for ≥2 value producers (spec-value-baseline §11.1).
    "value_baseline": "hygiene",
}


def category_for_producer(producer: str) -> Category:
    """Look up the default category for a producer name. Falls back to
    'platform' so a freshly-added producer surfaces somewhere instead of
    silently dropping off the categorized view."""
    return PRODUCER_CATEGORY_DEFAULT.get(producer, "platform")


# Producer is open-ended (string), not a Literal — new monitors should
# be addable without a schema bump. The spec lists the v1 producers but
# the store doesn't enforce membership.

SIGNAL_SCHEMA_VERSION = 1


# ─────────────────────────────────────────────────────────────────────────────
# State transition entry (audit trail)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class StateTransition:
    """An entry in a Signal's append-only state history."""

    from_state: State | None  # None for the create event
    to_state: State
    at: str  # ISO8601 UTC
    actor: str  # "producer" | "user:<id>" | "timer" | "<producer_name>"
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "from_state": self.from_state,
            "to_state": self.to_state,
            "at": self.at,
            "actor": self.actor,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StateTransition":
        return cls(
            from_state=data.get("from_state"),
            to_state=data["to_state"],
            at=data["at"],
            actor=data["actor"],
            reason=data.get("reason", ""),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Delivery record
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Delivery:
    """One dispatch attempt of this Signal to a notification channel."""

    channel: str  # "telegram" | "slack" | "log" | etc.
    at: str  # ISO8601 UTC
    suppressed_reason: str | None = None  # set when delivery was filtered out

    def to_dict(self) -> dict:
        return {
            "channel": self.channel,
            "at": self.at,
            "suppressed_reason": self.suppressed_reason,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Delivery":
        return cls(
            channel=data["channel"],
            at=data["at"],
            suppressed_reason=data.get("suppressed_reason"),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Remediation — a structured "Run fix" action a Signal can advertise
# ─────────────────────────────────────────────────────────────────────────────
#
# Producers attach a Remediation when they know the action that would
# resolve the signal. The admin UI renders a "Run fix" button and posts
# the {kind, params} to /api/admin/remediation/execute, which dispatches
# to a server-side handler keyed by ``kind``. Free-text ``fix_cmd``
# strings are a legacy fallback (display-only); Remediation is the
# typed, server-executable path.


@dataclass
class Remediation:
    """A structured remediation action a Signal advertises.

    ``kind`` selects the server-side handler. ``params`` is the
    handler-specific argument bundle (kept as a plain dict so producers
    don't import handler modules). ``label`` is the button text shown in
    the UI; ``confirm`` is the modal body text shown before execution.
    """

    kind: str  # e.g. "install_infra_jobs" | "reset_baseline" | "flip_cron_session_target"
    params: dict[str, Any] = field(default_factory=dict)
    label: str = ""
    confirm: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "params": dict(self.params),
            "label": self.label,
            "confirm": self.confirm,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Remediation":
        return cls(
            kind=data["kind"],
            params=dict(data.get("params") or {}),
            label=data.get("label", ""),
            confirm=data.get("confirm", ""),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Signal — the central type
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Signal:
    """An observation emitted by a monitor.

    Spec: docs/spec-alerts-signal-store-2026-05-07.md §4.
    """

    # ── Identity ────────────────────────────────────────────────────────────
    id: str
    signature: str  # producer:type:scope_key — for find-or-create dedup

    # ── Origin ──────────────────────────────────────────────────────────────
    producer: str
    type: str  # producer-namespaced, e.g. "cost_spike"
    flavor: Flavor
    severity: Severity
    scope: Scope
    bot_id: str | None = None  # null for pod/host scope
    # High-level domain bucket. Defaults to the per-producer mapping in
    # PRODUCER_CATEGORY_DEFAULT; producers can pass a different value to
    # override (e.g. an audit finding that's really a security issue can
    # be routed to "security" instead of audit's default "platform").
    # __post_init__ fills this from the producer when left empty so
    # callers don't have to thread it through every observe() call.
    category: Category | None = None

    # ── Body ────────────────────────────────────────────────────────────────
    title: str = ""
    body: str = ""
    # ``details`` carries producer-specific structured data. Two keys are
    # rendered specially by the Alerts UI and should be populated by
    # every operator-facing producer:
    #   ``what_it_means`` (str) — 1-3 sentence plain-English description
    #     of why the signal is firing and what it actually implies. Shown
    #     as a "What this means" block above raw details.
    #   ``fix_steps`` (str) — copy-pasteable numbered remediation steps
    #     ("1. …\n2. …"). Rendered as an "How to fix" ordered list.
    # See the audit.py and monitor_coverage.py producers for reference
    # implementations. Producers that omit these get a plain
    # title+body+raw-details rendering, which works but gives the
    # operator no triage guidance.
    details: dict[str, Any] = field(default_factory=dict)

    # ── Lifecycle ───────────────────────────────────────────────────────────
    state: State = "firing"
    created_at: str = field(default_factory=_utc_now_iso)
    last_observed_at: str = field(default_factory=_utc_now_iso)
    observation_count: int = 1
    snoozed_until: str | None = None
    resolved_at: str | None = None
    state_history: list[StateTransition] = field(default_factory=list)

    # ── Cross-links ─────────────────────────────────────────────────────────
    # Denormalized for display; canonical link lives on Proposal.motivating_signals.
    motivated_proposals: list[str] = field(default_factory=list)

    # ── Config shortcut ─────────────────────────────────────────────────────
    # Optional hint for the UI to render a direct "Configure" link on the
    # alert card. Shape: {generator: str, param: str, bot_id: str | None}.
    # Only set when a specific generator config key is the natural response
    # to the alert (e.g. raising a budget cap after a cost_alert fires).
    config_hint: dict[str, Any] | None = None

    # ── Remediation ─────────────────────────────────────────────────────────
    # Structured "Run fix" action — server dispatches by Remediation.kind.
    # See Remediation dataclass above. Optional; alerts without an obvious
    # mechanical fix leave this None and rely on the body/details to direct
    # the operator.
    remediation: Remediation | None = None

    # ── Coalescing ──────────────────────────────────────────────────────────
    # incident_key is a slug producers set when multiple signals are
    # symptoms of the same incident (e.g. "security_bot-embedding-storm" for the
    # 429 storm + gateway flap + high-cost session cluster). The alerts
    # renderer groups signals with the same incident_key within a time
    # window. Loose coupling — no validation across producers, no
    # cross-signal lookups at emit time.
    incident_key: str | None = None
    # caused_by_signal_id is a precise upstream link for cases where the
    # producer can identify a specific causing signal id. Set sparingly;
    # most producers should use incident_key instead.
    caused_by_signal_id: str | None = None

    # ── Delivery ────────────────────────────────────────────────────────────
    deliveries: list[Delivery] = field(default_factory=list)

    # ── Misc ────────────────────────────────────────────────────────────────
    schema_version: int = SIGNAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Signal.id must be non-empty")
        if not self.signature:
            raise ValueError("Signal.signature must be non-empty")
        if not self.producer:
            raise ValueError("Signal.producer must be non-empty")
        if not self.type:
            raise ValueError("Signal.type must be non-empty")
        if self.scope == "bot" and not self.bot_id:
            raise ValueError("Signal.bot_id must be set when scope='bot'")
        # Fill category from the producer when not explicitly provided —
        # producers don't have to thread it through every observe() call.
        if not self.category:
            self.category = category_for_producer(self.producer)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "schema_version": self.schema_version,
            "signature": self.signature,
            "producer": self.producer,
            "type": self.type,
            "flavor": self.flavor,
            "severity": self.severity,
            "scope": self.scope,
            "bot_id": self.bot_id,
            "category": self.category,
            "title": self.title,
            "body": self.body,
            "details": dict(self.details),
            "state": self.state,
            "created_at": self.created_at,
            "last_observed_at": self.last_observed_at,
            "observation_count": self.observation_count,
            "snoozed_until": self.snoozed_until,
            "resolved_at": self.resolved_at,
            "state_history": [h.to_dict() for h in self.state_history],
            "motivated_proposals": list(self.motivated_proposals),
            "deliveries": [d.to_dict() for d in self.deliveries],
            "config_hint": self.config_hint,
            "remediation": self.remediation.to_dict() if self.remediation else None,
            "incident_key": self.incident_key,
            "caused_by_signal_id": self.caused_by_signal_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Signal":
        rem_data = data.get("remediation")
        # Backward compat: signals stored before the category field landed
        # get one derived from the producer at load time.
        category = data.get("category") or category_for_producer(data["producer"])
        return cls(
            id=data["id"],
            schema_version=int(data.get("schema_version", SIGNAL_SCHEMA_VERSION)),
            signature=data["signature"],
            producer=data["producer"],
            type=data["type"],
            flavor=data["flavor"],
            severity=data["severity"],
            scope=data["scope"],
            bot_id=data.get("bot_id"),
            category=category,
            title=data.get("title", ""),
            body=data.get("body", ""),
            details=dict(data.get("details") or {}),
            state=data.get("state", "firing"),
            created_at=data.get("created_at", _utc_now_iso()),
            last_observed_at=data.get("last_observed_at", _utc_now_iso()),
            observation_count=int(data.get("observation_count", 1)),
            snoozed_until=data.get("snoozed_until"),
            resolved_at=data.get("resolved_at"),
            state_history=[
                StateTransition.from_dict(h)
                for h in (data.get("state_history") or [])
            ],
            motivated_proposals=list(data.get("motivated_proposals") or []),
            deliveries=[
                Delivery.from_dict(d) for d in (data.get("deliveries") or [])
            ],
            config_hint=data.get("config_hint"),
            remediation=Remediation.from_dict(rem_data) if rem_data else None,
            incident_key=data.get("incident_key"),
            caused_by_signal_id=data.get("caused_by_signal_id"),
        )


def new_signal_id() -> str:
    """Generate a fresh signal ID."""
    return str(uuid4())


def make_signature(producer: str, type_: str, scope_key: str) -> str:
    """Build a canonical signature string for find-or-create dedup.

    ``scope_key`` is whatever uniquely identifies the affected resource
    within the producer's domain — e.g. a bot_id for per-bot signals,
    "mini" for host-scoped, "all" for pod-wide. Producers should keep
    this stable across runs so the dedup key is consistent.
    """
    return f"{producer}:{type_}:{scope_key}"
