"""Operator-facing Subscription registry — Phase 2 of subscription completeness.

Phase 1 made every dispatched message bind to a ``CatalogEvent`` (~57
internal events). But ``catalog_event`` == the operator-facing toggle 1:1,
yielding ~65 switches — far too many. This module adds a **grouping layer
ABOVE the events**: 13 operator-facing *Subscriptions*, each gathering a
coherent set of ``CatalogEvent``s.

The two-layer model (spec-subscription-completeness-2026-06-24.md, Phase 2):

  * **Internal events (~57)** — KEEP. They drive routing, dedup, digest
    rollup, body templates, producer mapping. Each ``CatalogEvent`` declares
    a ``subscription`` membership (or ``None`` for the 8 internal/removed
    events that are not operator subscriptions).
  * **Operator Subscriptions (13)** — defined here. **Gating moves from
    per-event to per-Subscription.** Configure lists 13; the Messages-tab
    provenance shows an event's Subscription + the group toggle.

This module is the single source of truth for the 13 Subscriptions. The
event → subscription edges live on ``CatalogEvent.subscription`` in
``catalog.py``; the totality ratchet (test_alerts_subscription_groups)
enforces that every non-internal event maps to exactly one Subscription id
defined here, and that the 13 ids are exactly the spec's set.

Storage / gating: the operator's per-Subscription on/off lands in
``{shared_dir}/alerts/subscriptions.json`` under
``subscription_groups.<id>.enabled`` (see ``subscriptions.py``). Absent →
``default_enabled``. The dispatcher resolves event → subscription →
group-enabled to decide whether a message is gated off.
"""

from __future__ import annotations

from dataclasses import dataclass


# ── Subscription record ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class OperatorSubscription:
    """One operator-facing Subscription — a coherent group of CatalogEvents.

    ``id`` is the stable snake_case handle persisted in the operator's
    config and referenced by ``CatalogEvent.subscription``. ``label`` /
    ``description`` are the Configure-tab presentation. ``default_enabled``
    is the on/off default before any operator override. ``is_safety_critical``
    marks a group containing a "blind-to-blindness" member — disabling it
    silences a safety alert, so the UI (chip 2) warns on mute. Safety-critical
    does NOT block the toggle; maximum operator control is preserved.
    """

    id: str
    label: str
    description: str
    default_enabled: bool = True
    is_safety_critical: bool = False


# ── The 13 (operator-approved taxonomy, spec Phase 2) ───────────────────────
#
# Display order is operator-IA: decisions first (most actionable), then
# security, autonomy, the two cost groups, the three system-health groups
# (deliberately NOT merged — operator decision), config, updates, the
# reports, the alerting-system meta group, and upstream tracking last
# (dev-profile, default OFF).

SUBSCRIPTIONS: tuple[OperatorSubscription, ...] = (
    OperatorSubscription(
        id="needs_decision",
        label="Needs your decision",
        description=(
            "A self-improvement proposal or an app build is ready and "
            "waiting for your review and approval."
        ),
    ),
    OperatorSubscription(
        id="security_findings",
        label="Security findings",
        description=(
            "New security issues affecting your pod — audit findings, "
            "CVEs matching your software, configuration drift, and overdue "
            "key rotation."
        ),
        is_safety_critical=True,
    ),
    OperatorSubscription(
        id="autonomy",
        label="Bot permissions & autonomy",
        description=(
            "Changes to what your bots are allowed to do on their own — "
            "posture drift, freedom wider than the default, daily limits "
            "hit, automatic step-downs, and clean-record promotion "
            "candidates."
        ),
    ),
    OperatorSubscription(
        id="cost_warnings",
        label="Cost warnings",
        description=(
            "Heads-up spend signals with no enforcement — soft daily "
            "threshold crossings, short-window bursts, heartbeat session "
            "bloat, and the weekly spend summary."
        ),
    ),
    OperatorSubscription(
        id="cost_enforcement",
        label="Cost enforcement",
        description=(
            "Cost controls that actually changed a bot's behavior — hard "
            "cap hits, breaker trips, tier downgrades, gateway stops, and "
            "per-session budget pauses. Disabling this silences alerts "
            "about active enforcement."
        ),
        is_safety_critical=True,
    ),
    OperatorSubscription(
        id="pod_health",
        label="Pod & gateway health",
        description=(
            "Pod- and host-level health — gateway up/down, failed "
            "auto-restarts, watchdog warnings, daemon error spikes, a "
            "wedged code-pull service, stalled scheduled jobs, dormant "
            "sudo grants, and stuck proposals."
        ),
        is_safety_critical=True,
    ),
    OperatorSubscription(
        id="app_delivery",
        label="App & message delivery",
        description=(
            "Whether your bots' scheduled apps and messages are actually "
            "reaching you — missed or unverifiable deliveries, a pod-wide "
            "delivery regression, a broken send surface after an update, "
            "manifest validation errors, and failed app installs."
        ),
        is_safety_critical=True,
    ),
    OperatorSubscription(
        id="bot_config",
        label="Bot configuration & plugins",
        description=(
            "Bot configuration and plugin posture — plugin health issues, "
            "failing exec/tool outcomes, and outdated OpenClaw CLI calls."
        ),
    ),
    OperatorSubscription(
        id="updates",
        label="Software updates",
        description=(
            "Available software updates — new OpenClaw releases (and "
            "whether they're safe to install), surface changes Evolve "
            "depends on, new Evolve code, and plugin updates."
        ),
    ),
    OperatorSubscription(
        id="daily_report",
        label="Daily pod report",
        description=(
            "The once-daily pod-wide summary: yesterday's usage and spend, "
            "today's operational issues, and newly-arrived self-improvement "
            "items."
        ),
    ),
    OperatorSubscription(
        id="weekly_summaries",
        label="Weekly summaries",
        description=(
            "Weekly roll-ups — the self-improvement review and per-bot "
            "trend chips (Sundays)."
        ),
    ),
    OperatorSubscription(
        id="alerting_system",
        label="Alerting system",
        description=(
            "Health of the alerting system itself — storm-mode "
            "rate-limiting and dispatcher health. Disabling this can hide "
            "the fact that other alerts are being suppressed or failing to "
            "deliver."
        ),
        is_safety_critical=True,
    ),
    OperatorSubscription(
        id="upstream_tracking",
        label="Upstream issue tracking",
        description=(
            "Activity on GitHub issues you filed against upstream projects "
            "Evolve watches. A developer-profile feature — off by default."
        ),
        default_enabled=False,
    ),
)


# The canonical id set — used by the totality ratchet to assert the
# catalog references exactly these and no orphans/typos.
SUBSCRIPTION_IDS: frozenset[str] = frozenset(s.id for s in SUBSCRIPTIONS)


# ── Lookups ─────────────────────────────────────────────────────────────────


_BY_ID: dict[str, OperatorSubscription] = {s.id: s for s in SUBSCRIPTIONS}


def by_id(subscription_id: str) -> OperatorSubscription | None:
    """Return the Subscription with that id, or None if unknown."""
    return _BY_ID.get(subscription_id)


def all_subscriptions() -> tuple[OperatorSubscription, ...]:
    """Return all 13 Subscriptions in operator-IA display order."""
    return SUBSCRIPTIONS


def all_ids() -> frozenset[str]:
    return SUBSCRIPTION_IDS


__all__ = (
    "OperatorSubscription",
    "SUBSCRIPTIONS",
    "SUBSCRIPTION_IDS",
    "by_id",
    "all_subscriptions",
    "all_ids",
)
