"""schema — declarative inventory of every operator-tunable configuration key.

The schema is the API contract between the install's configuration surface,
the sandbox UI, and RSI proposals. Adding or removing a key here is a
deliberate change with downstream consequences: the customizations UI starts
or stops surfacing it, RSI starts or stops being able to propose changes
through ConfigPatch.

Each entry declares:

  path          Logical dotted identifier. Stable across file reorganization;
                used as the primary key in the provenance index.
  store         Logical backing-store name. Maps to a physical file path via
                the resolver in `stores.py` (added in step 2).
  target_path   ConfigPatch-compatible address of the form ``{file}::{dotted.key}``,
                or just ``{file}`` for whole-document keys (prose). This is the
                address an applier or operator UI uses to write the value.
  strength      free / advisory / shipped_policy. See Strength docstring.
  stock_default Compiled-in default. The cascade base; what the install gets
                if it has not overridden the key.
  type_hint     One of: bool, int, float, string, enum, list, doc.
  description   One line, operator-facing. What the key controls and (for
                shipped_policy) why we tune it centrally.

Some keys live "out of sandbox" — model→tier mapping (driven by the primary
bot's evolve-tiers.json), generator charters (fingerprint-locked), API keys
(secret stores). They are intentionally absent from this schema. Identity-
category keys (network.json structure, SOUL.md, manifests/) ARE in the schema
because the customizations UI needs to see them, but their strength is `free`
and the cascade is structurally trivial (no shipped default to propagate).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any


class Strength(IntEnum):
    """Override-strength label.

    Stored as a small int so RSI ranking is cheap; rendered to operators
    as the matching label. The label tells the operator what overriding
    *costs* — it does not gate the override.

    FREE (0)
        Local economics, identity, escape hatches, feature flags. No
        globally optimal value exists. Override freely; new shipped
        defaults still flow through unrelated keys.

    ADVISORY (1)
        We have a default that's reasonable but not telemetry-informed.
        Override is fine; on upgrade you'll see if our default moved.

    SHIPPED_POLICY (2)
        Defaults we are actively tuning across installs from telemetry.
        Overriding here means you stop benefiting from that learning.
        RSI proposals targeting a shipped-policy key should default to
        upstream-PR rather than per-install override.
    """

    FREE = 0
    ADVISORY = 1
    SHIPPED_POLICY = 2

    @property
    def label(self) -> str:
        return {0: "free", 1: "advisory", 2: "shipped-policy"}[int(self)]


# Logical backing-store names. The physical file path is resolved by the
# stores module — this layer is just the categorical tag.
#
# Per-bot generator tunables (e.g. efficiency_hawk.cost.*) are deliberately
# NOT in this schema. They live in ``_GENERATOR_TUNABLE_PARAMS`` and the
# matching POST endpoints in ``evolve_admin.web.server`` — that system has
# UI metadata (label, help, unit, step, decimals) and live write paths the
# sandbox doesn't replicate. The sandbox's customizations() does not cover
# generator-store keys; the existing per-bot generator-config UI is the
# authoritative surface for those. A future PR can unify the two by adding
# UI metadata to TunableKey and projecting _GENERATOR_TUNABLE_PARAMS from
# this schema. Until then, the boundary is: sandbox owns
# install-config-at-large; _GENERATOR_TUNABLE_PARAMS owns generator detector
# tunables.
class Store:
    NETWORK = "network"                     # {shared_dir}/network.json
    BETTER_ENGINE = "better_engine"         # {shared_dir}/better-engine-config.json
    BOT_GUIDE = "bot_guide"                 # {shared_dir}/bot_guides/<id>.md
    OPENCLAW = "openclaw"                   # ~<bot>/.openclaw/openclaw.json
    EVOLVE_TIERS = "evolve_tiers"           # ~<bot>/.openclaw/evolve-tiers.json
    SOUL = "soul"                           # ~<bot>/.openclaw/SOUL.md
    AGENTS = "agents"                       # ~<bot>/.openclaw/AGENTS.md
    MANIFEST = "manifest"                   # ~<bot>/.openclaw/manifests/<id>.md


@dataclass(frozen=True)
class TunableKey:
    path: str
    store: str
    target_path: str
    strength: Strength
    stock_default: Any
    type_hint: str
    description: str
    # Whether the key is parameterized per-bot. When True, target_path uses
    # the literal token ``<bot_id>`` which the resolver substitutes.
    per_bot: bool = False
    # Whether the key is parameterized per-generator (for GENERATOR-store
    # entries that follow a per-generator pattern).
    per_generator: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

# The schema is grouped by store for readability. Order within a group is
# the order keys appear in the source file when possible.

_NETWORK: tuple[TunableKey, ...] = (
    TunableKey(
        path="network.timezone",
        store=Store.NETWORK,
        target_path="network.json::timezone",
        strength=Strength.FREE,
        stock_default="UTC",
        type_hint="string",
        description="Pod timezone for daily rollups and scheduled jobs.",
    ),
    TunableKey(
        path="network.thresholds.dailySpendAlertUsd",
        store=Store.NETWORK,
        target_path="network.json::thresholds.dailySpendAlertUsd",
        strength=Strength.FREE,
        stock_default=5.0,
        type_hint="float",
        description="Daily spend at which the operator gets an alert.",
    ),
    TunableKey(
        path="network.thresholds.weeklySpendAlertUsd",
        store=Store.NETWORK,
        target_path="network.json::thresholds.weeklySpendAlertUsd",
        strength=Strength.FREE,
        stock_default=20.0,
        type_hint="float",
        description="Weekly spend alert ceiling.",
    ),
    TunableKey(
        path="network.thresholds.spendCapAction",
        store=Store.NETWORK,
        target_path="network.json::thresholds.spendCapAction",
        strength=Strength.ADVISORY,
        stock_default="downgrade-tier",
        type_hint="enum",
        description=(
            "Action when the spend cap is hit. 'downgrade-tier' (recommended) "
            "or 'alert-only'."
        ),
    ),
    TunableKey(
        path="network.thresholds.maxSessionContextTokens",
        store=Store.NETWORK,
        target_path="network.json::thresholds.maxSessionContextTokens",
        strength=Strength.ADVISORY,
        stock_default=100000,
        type_hint="int",
        description="Per-session context-token ceiling. Provider-dependent.",
    ),
    TunableKey(
        path="network.classifiers.tier.fallback",
        store=Store.NETWORK,
        target_path="network.json::classifiers.tier.fallback",
        strength=Strength.ADVISORY,
        stock_default="keyword",
        type_hint="enum",
        description="Fallback classifier when the LLM tier classifier is unavailable.",
    ),
    TunableKey(
        path="network.alerts.spendAlertHour",
        store=Store.NETWORK,
        target_path="network.json::alerts.spendAlertHour",
        strength=Strength.FREE,
        stock_default=12,
        type_hint="int",
        description="Hour of day (0-23) for the daily spend alert.",
    ),
    TunableKey(
        path="network.alerts.enabled",
        store=Store.NETWORK,
        target_path="network.json::alerts.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Master switch for operator alerts.",
    ),
    TunableKey(
        path="network.alerts.cronSilenceThresholdDays",
        store=Store.NETWORK,
        target_path="network.json::alerts.cronSilenceThresholdDays",
        strength=Strength.ADVISORY,
        stock_default=2,
        type_hint="int",
        description="Days a watched cron may be silent before alerting.",
    ),
    TunableKey(
        path="network.alerts.watchedCrons",
        store=Store.NETWORK,
        target_path="network.json::alerts.watchedCrons",
        strength=Strength.ADVISORY,
        stock_default=["ai.evolve.*.measure", "ai.evolve.*.heal"],
        type_hint="list",
        description="Cron-label patterns the silence detector watches.",
    ),
    TunableKey(
        path="network.security.mode",
        store=Store.NETWORK,
        target_path="network.json::security.mode",
        strength=Strength.ADVISORY,
        stock_default="primary",
        type_hint="enum",
        description=(
            "Where review.py runs. 'primary' = on the primary bot; "
            "'dedicated' = on security.botId."
        ),
    ),
    TunableKey(
        path="network.security.autoRejectRisk",
        store=Store.NETWORK,
        target_path="network.json::security.autoRejectRisk",
        strength=Strength.SHIPPED_POLICY,
        stock_default=["high", "critical"],
        type_hint="list",
        description=(
            "Risk levels that are auto-rejected without operator review. "
            "Tuned across installs from security telemetry — overriding "
            "means you stop receiving improvements to this floor."
        ),
    ),
    TunableKey(
        path="network.models.routing.maintenanceTier",
        store=Store.NETWORK,
        target_path="network.json::models.routing.maintenanceTier",
        strength=Strength.ADVISORY,
        stock_default="tier3",
        type_hint="enum",
        description="Default tier for maintenance-class sessions.",
    ),
    TunableKey(
        path="network.models.routing.backgroundTier",
        store=Store.NETWORK,
        target_path="network.json::models.routing.backgroundTier",
        strength=Strength.ADVISORY,
        stock_default="tier3",
        type_hint="enum",
        description="Default tier for background-class sessions.",
    ),
    TunableKey(
        path="network.models.routing.ambiguousTier",
        store=Store.NETWORK,
        target_path="network.json::models.routing.ambiguousTier",
        strength=Strength.ADVISORY,
        stock_default=None,
        type_hint="enum",
        description="Tier for ambiguous sessions; null means use bot default.",
    ),
    TunableKey(
        path="network.accounts.routing.enabled",
        store=Store.NETWORK,
        target_path="network.json::accounts.routing.enabled",
        strength=Strength.FREE,
        stock_default=False,
        type_hint="bool",
        description="Enable account-tier routing.",
    ),
)


_BETTER_ENGINE: tuple[TunableKey, ...] = (
    TunableKey(
        path="better_engine.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.better_engine.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Master switch for Better Engine.",
        per_bot=True,
    ),
    TunableKey(
        path="better_engine.rsi.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.rsi.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="RSI layer on/off.",
        per_bot=True,
    ),
    TunableKey(
        path="better_engine.budget.monthly_cap_usd",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.budget.monthly_cap_usd",
        strength=Strength.FREE,
        stock_default=50.00,
        type_hint="float",
        description="Pod-wide monthly spend cap.",
    ),
    TunableKey(
        path="better_engine.budget.per_bot_daily_warn_usd",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.budget.per_bot_daily_warn_usd",
        strength=Strength.FREE,
        stock_default=2.00,
        type_hint="float",
        description="Per-bot daily-spend warning threshold.",
        per_bot=True,
    ),
    TunableKey(
        path="better_engine.budget.per_bot_daily_hard_usd",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.budget.per_bot_daily_hard_usd",
        strength=Strength.FREE,
        stock_default=5.00,
        type_hint="float",
        description="Per-bot daily-spend hard cap.",
        per_bot=True,
    ),
    TunableKey(
        path="better_engine.budget.per_bot_monthly_cap_usd",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.budget.per_bot_monthly_cap_usd",
        strength=Strength.FREE,
        stock_default=None,
        type_hint="float",
        description="Per-bot monthly cap; null derives from daily values.",
        per_bot=True,
    ),
    TunableKey(
        path="better_engine.conversational_approval.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.conversational_approval.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Conversational approval flow on/off (operator escape hatch).",
    ),
    TunableKey(
        path="better_engine.conversational_approval.llm_intent_parse_enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.conversational_approval.llm_intent_parse_enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Stage-2 LLM intent parser on/off (cost-sensitive opt-out).",
    ),
    TunableKey(
        path="better_engine.conversational_approval.confidence_threshold",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.conversational_approval.confidence_threshold",
        strength=Strength.SHIPPED_POLICY,
        stock_default=0.80,
        type_hint="float",
        description=(
            "Confidence floor for treating an intent parse as decisive. "
            "Tuned from telemetry — overriding stops you receiving improvements."
        ),
    ),
    TunableKey(
        path="better_engine.conversational_approval.default_snooze_days",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.conversational_approval.default_snooze_days",
        strength=Strength.SHIPPED_POLICY,
        stock_default=3,
        type_hint="int",
        description="Default snooze period when the user gives no duration hint.",
    ),
    TunableKey(
        path="better_engine.conversational_approval.pending_expiry_minutes",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.conversational_approval.pending_expiry_minutes",
        strength=Strength.SHIPPED_POLICY,
        stock_default=60,
        type_hint="int",
        description="Minutes a pending rec_pending session lingers before expiry.",
    ),
    TunableKey(
        path="better_engine.conversational_approval.push_preamble_enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.conversational_approval.push_preamble_enabled",
        strength=Strength.FREE,
        stock_default=False,
        type_hint="bool",
        description="Push 'before we dive in' preambles for unrelated turns.",
    ),
)


# Generator detector tunables are deliberately NOT in this schema; see the
# Store-class docstring for the rationale. They live in
# ``_GENERATOR_TUNABLE_PARAMS`` in ``evolve_admin.web.server``.


# ── alerts.* — sysadmin-alert dispatcher toggles ────────────────────────────
# Phase 2 of docs/spec-alert-notifier-2026-05-09.md. These entries surface
# enable/disable + cooldown knobs for every push source in the admin UI's
# config page. The alerts.dispatcher library reads them via config_sandbox
# and falls back to compiled-in defaults if a key is unset
# (alerts/_config_lookup.py + alerts/dispatcher.py default tables — kept
# in sync with this schema so behavior is identical pre- and post-Phase-2).
_ALERTS: tuple[TunableKey, ...] = (
    TunableKey(
        path="alerts.dispatcher_enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.dispatcher_enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Master switch for sysadmin push alerts. When off, no source can push.",
    ),
    # Per-source enables — all default-on. signal_notifier shipped default-off
    # in v1 and was flipped to stock_default=True on 2026-05-20 (Phase 7,
    # after the burn-in); the "opt-in v1" note that used to live here was
    # stale for two months and misled a live-silence investigation on
    # 2026-07-29.
    TunableKey(
        path="alerts.audit.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.audit.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Push security-audit findings to the operator's chat channel.",
    ),
    TunableKey(
        path="alerts.pod_report.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.pod_report.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Push the daily pod-report summary to the operator's chat channel.",
    ),
    TunableKey(
        path="alerts.spend_alert.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.spend_alert.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Push intraday per-bot spend-threshold breaches.",
    ),
    TunableKey(
        path="alerts.cron_alert.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.cron_alert.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Push dead-man's-switch alerts when a watched cron stops firing.",
    ),
    TunableKey(
        path="alerts.forge_engine.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.forge_engine.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Push forge-engine job completion notifications.",
    ),
    TunableKey(
        path="alerts.signal_notifier.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.signal_notifier.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description=(
            "Push Signal-store transitions (gateway up/down, host metrics, "
            "integration probes) to the operator's chat channel. Lands "
            "Phase 7 of docs/spec-alert-notifier-2026-05-09.md — flipped "
            "on 2026-05-20 after the planned burn-in window."
        ),
    ),
    # Phase C migration — heal.py source-level toggle (covers config drift,
    # gateway autorestart failures, watchdog events).
    TunableKey(
        path="alerts.heal.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.heal.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Push heal.py self-healing alerts (config drift, autorestart failed, watchdog).",
    ),
    TunableKey(
        path="alerts.heal.cooldown_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.heal.cooldown_seconds",
        strength=Strength.FREE,
        stock_default=86_400,
        type_hint="int",
        description=(
            "Per-dedup-key cooldown for heal alerts. Default 24h: "
            "gateway-down / config-drift / watchdog conditions persist "
            "while heal ticks every 5 min, so a shorter cooldown would "
            "re-page the operator on the same condition (the 2026-05-31 "
            "gateway-spam pattern). Identical-content suppression provides "
            "a 24h floor regardless, so lowering this knob only widens "
            "the *different*-content alert window."
        ),
    ),
    # Phase C migration — review.py source-level toggle (covers proposal
    # rejected / reviewed / quarantined alerts).
    TunableKey(
        path="alerts.review.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.review.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Push review.py proposal-lifecycle alerts (rejected, reviewed, quarantined).",
    ),
    TunableKey(
        path="alerts.review.cooldown_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.review.cooldown_seconds",
        strength=Strength.FREE,
        stock_default=0,
        type_hint="int",
        description="Cooldown for review alerts (per-proposal dedup_keys handle uniqueness; default 0).",
    ),
    # Phase C migration — cost.py source-level toggle (key rotation overdue
    # alerts only; the cost.py spend-threshold path is retired in this
    # migration because it duplicates spend_alert).
    TunableKey(
        path="alerts.cost.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.cost.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Push cost.py alerts (currently: key rotation overdue).",
    ),
    TunableKey(
        path="alerts.cost.cooldown_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.cost.cooldown_seconds",
        strength=Strength.FREE,
        stock_default=86_400,
        type_hint="int",
        description=(
            "Cooldown for cost.py alerts. Default 24h: key-rotation-"
            "overdue is a persisting condition (same stale-bot set = "
            "same fingerprint dedup_key), so a STATE_PERSISTS-style "
            "floor matches the producer's semantics."
        ),
    ),
    # cost_watchdog source — heartbeat-session-bloat and model-override-leak
    # detections. The producer's dedup_key embeds `{today}`, so a 24h source
    # cooldown gives "once per (bot, session, type) per day" — matching the
    # spend_alert / cron_alert pattern. Without it, hourly cost_watchdog
    # runs re-fire the same alert every tick (caught 2026-05-24 — bot
    # `security_bot` heartbeats spammed chat at 1/hr each).
    TunableKey(
        path="alerts.cost_watchdog.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.cost_watchdog.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Push cost_watchdog alerts (heartbeat-session bloat, model-override leak).",
    ),
    TunableKey(
        path="alerts.cost_watchdog.cooldown_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.cost_watchdog.cooldown_seconds",
        strength=Strength.FREE,
        stock_default=86_400,
        type_hint="int",
        description=(
            "Cooldown for cost_watchdog alerts. Default 24h, matching the "
            "producer's daily dedup_key. Lower this only if the producer's "
            "cadence changes."
        ),
    ),
    # Phase C migration — proposal lifecycle: analyze (proposals ready),
    # apply (apply result), outcome (post-apply checkin).
    TunableKey(
        path="alerts.analyze.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.analyze.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Push analyze.py 'N new proposals ready for review' batches.",
    ),
    TunableKey(
        path="alerts.analyze.cooldown_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.analyze.cooldown_seconds",
        strength=Strength.FREE,
        stock_default=0,
        type_hint="int",
        description="Cooldown for analyze alerts (per-batch dedup_keys; analyze runs weekly).",
    ),
    TunableKey(
        path="alerts.apply.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.apply.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Push apply.py 'Apply Result' notifications (success / rollback / failure).",
    ),
    TunableKey(
        path="alerts.apply.cooldown_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.apply.cooldown_seconds",
        strength=Strength.FREE,
        stock_default=0,
        type_hint="int",
        description="Cooldown for apply alerts (per-proposal dedup_keys handle uniqueness).",
    ),
    TunableKey(
        path="alerts.outcome.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.outcome.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Push outcome.py 7-day post-apply 'did this help?' check-in messages.",
    ),
    TunableKey(
        path="alerts.outcome.cooldown_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.outcome.cooldown_seconds",
        strength=Strength.FREE,
        stock_default=0,
        type_hint="int",
        description="Cooldown for outcome check-in alerts (per-outcome_id dedup_keys).",
    ),
    # Phase C final five — ad-hoc reports, test runner, validate, weekly
    # review, repo-puller wedge.
    TunableKey(
        path="alerts.report.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.report.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Push ad-hoc analyzer reports (operator-invoked).",
    ),
    TunableKey(
        path="alerts.report.cooldown_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.report.cooldown_seconds",
        strength=Strength.FREE,
        stock_default=0,
        type_hint="int",
        description="Cooldown for ad-hoc analyzer reports.",
    ),
    TunableKey(
        path="alerts.test_runner.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.test_runner.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Push weekly regression-suite failure alerts.",
    ),
    TunableKey(
        path="alerts.test_runner.cooldown_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.test_runner.cooldown_seconds",
        strength=Strength.FREE,
        stock_default=86_400,
        type_hint="int",
        description=(
            "Cooldown for test-runner alerts. Default 24h: same-"
            "failures fingerprint persists across weekly runs; the "
            "STATE_PERSISTS floor catches the case where failures "
            "repeat unchanged."
        ),
    ),
    TunableKey(
        path="alerts.validate.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.validate.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Push forge/manifest validation results (pass / fail / needs-human / error).",
    ),
    TunableKey(
        path="alerts.validate.cooldown_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.validate.cooldown_seconds",
        strength=Strength.FREE,
        stock_default=0,
        type_hint="int",
        description="Cooldown for validate alerts (per-proposal dedup_keys).",
    ),
    TunableKey(
        path="alerts.weekly_review.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.weekly_review.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Push the weekly RSI process-health report.",
    ),
    TunableKey(
        path="alerts.weekly_review.cooldown_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.weekly_review.cooldown_seconds",
        strength=Strength.FREE,
        stock_default=86400,
        type_hint="int",
        description="Cooldown for weekly-review alerts. 24h floor on the "
        "per-ISO-week dedup_key (STATE_PERSISTS) so a launchd make-up run or "
        "deploy-triggered re-fire can't re-send the same week's review; the "
        "producer's ISO-week sentinel is the full-week guard.",
    ),
    TunableKey(
        path="alerts.weekly_bot_trends.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.weekly_bot_trends.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Push the weekly per-bot 7-day trend digest (sustained cost spikes, sustained pushback, unexpected billing).",
    ),
    TunableKey(
        path="alerts.weekly_bot_trends.cooldown_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.weekly_bot_trends.cooldown_seconds",
        strength=Strength.FREE,
        stock_default=86400,
        type_hint="int",
        description="Cooldown for weekly-bot-trends alerts. 24h floor on the "
        "per-ISO-week dedup_key (STATE_PERSISTS) so a launchd make-up run or "
        "deploy-triggered re-fire can't re-send the same week's digest.",
    ),
    TunableKey(
        path="alerts.repo_puller.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.repo_puller.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Push repo-puller wedge notifications (deploy drifting from origin/main).",
    ),
    TunableKey(
        path="alerts.repo_puller.cooldown_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.repo_puller.cooldown_seconds",
        strength=Strength.FREE,
        stock_default=0,
        type_hint="int",
        description="Cooldown for repo-puller wedge alerts (per-wedge-issue dedup upstream).",
    ),
    # Phase E2 — update_watcher source: polls npm for openclaw releases
    # and origin/main for new evolve commits; emits via dispatcher when
    # new versions or commits are detected.
    TunableKey(
        path="alerts.update_watcher.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.update_watcher.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Push update_watcher notifications (OpenClaw new version, evolve repo updates).",
    ),
    TunableKey(
        path="alerts.update_watcher.cooldown_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.update_watcher.cooldown_seconds",
        strength=Strength.FREE,
        stock_default=0,
        type_hint="int",
        description="Cooldown for update_watcher alerts (per-version dedup_keys handle uniqueness).",
    ),
    # Power feature (developer profile) — upstream_issues_watcher source:
    # gh-CLI poller for maintainer activity on issues we filed. Gated at
    # install time by install.json::feature_profile; this knob is the
    # at-runtime kill switch the operator can flip without uninstalling
    # the launchd job.
    TunableKey(
        path="alerts.upstream_issues_watcher.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.upstream_issues_watcher.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Push upstream_issues_watcher notifications (maintainer comments / state changes on filed issues).",
    ),
    TunableKey(
        path="alerts.upstream_issues_watcher.cooldown_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.upstream_issues_watcher.cooldown_seconds",
        strength=Strength.FREE,
        stock_default=0,
        type_hint="int",
        description="Cooldown for upstream_issues_watcher alerts (per-comment dedup_keys handle uniqueness).",
    ),
    # Phase G — digest_dispatcher source: drains the daily/weekly digest
    # queue and pushes one bundled message per flush.
    TunableKey(
        path="alerts.digest_dispatcher.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.digest_dispatcher.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Push the bundled daily/weekly digest message (drains digest-pending queues).",
    ),
    TunableKey(
        path="alerts.digest_dispatcher.cooldown_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.digest_dispatcher.cooldown_seconds",
        strength=Strength.FREE,
        stock_default=0,
        type_hint="int",
        description="Cooldown for digest dispatch (dedup_key = flush date, naturally unique).",
    ),
    # L1 cost breaker auto-trip runner — wired 2026-05-28 after the
    # Security_bot trip incident. Trips are operator-critical state changes;
    # the operator can turn the push off if they prefer to discover
    # trips only via the admin UI, but the default is on.
    TunableKey(
        path="alerts.breakers_runner.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.breakers_runner.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Push breakers_runner notifications (L1 cost breaker auto-trips).",
    ),
    TunableKey(
        path="alerts.breakers_runner.cooldown_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.breakers_runner.cooldown_seconds",
        strength=Strength.FREE,
        stock_default=0,
        type_hint="int",
        description="Cooldown for breakers_runner alerts (per-trip dedup_keys handle uniqueness).",
    ),
    # Post-OC-upgrade end-to-end delivery proof (Wave-5 of the 2026-06-11
    # delivery P0): one real "your bots can still message you" send per
    # upgrade — the message itself is the probe.
    TunableKey(
        path="alerts.send_surface_probe.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.send_surface_probe.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Push the post-upgrade delivery-proof message (one real send per OpenClaw upgrade).",
    ),
    TunableKey(
        path="alerts.send_surface_probe.cooldown_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.send_surface_probe.cooldown_seconds",
        strength=Strength.FREE,
        stock_default=0,
        type_hint="int",
        description="Cooldown for the post-upgrade delivery proof (one send per upgrade; version in body dedups).",
    ),
    # Canary *soak* send-probe — decoupled from send_surface_probe so the
    # soak's operator-visibility is independent of the post-OC-upgrade proof
    # (which stays ON). DEFAULT-OFF: a healthy soak must be silent (the
    # verdict is read programmatically). Reserved surface for a future live
    # per-candidate re-target send; today soak_probe._probe_send emits
    # nothing on the live operator channel.
    TunableKey(
        path="alerts.soak_send_probe.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.soak_send_probe.enabled",
        strength=Strength.FREE,
        stock_default=False,
        type_hint="bool",
        description="Push the canary soak send-probe message (default off — a healthy soak is silent; the verdict is read programmatically).",
    ),
    TunableKey(
        path="alerts.soak_send_probe.cooldown_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.soak_send_probe.cooldown_seconds",
        strength=Strength.FREE,
        stock_default=0,
        type_hint="int",
        description="Cooldown for the canary soak send-probe (per-candidate dedup_keys handle uniqueness if ever enabled).",
    ),
    # Post-wrap app-install outcomes (U1 activation fix, 2026-06-11):
    # pack-install failures after the add-bot wrap + briefing
    # auto-activation on first channel connect. Per-job dedup_keys.
    TunableKey(
        path="alerts.app_install.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.app_install.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Push app-install outcome notifications (post-wrap install failures, briefing auto-activation).",
    ),
    TunableKey(
        path="alerts.app_install.cooldown_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.app_install.cooldown_seconds",
        strength=Strength.FREE,
        stock_default=0,
        type_hint="int",
        description="Cooldown for app-install outcome alerts (per-job dedup_keys handle uniqueness).",
    ),
    # Daily CVE scan findings (security-cve-scan/finalize.py →
    # security.cve_finding). Content-hashed dedup_key (sorted CVE-id list)
    # + 24h floor = one announcement per distinct finding-set per day,
    # the audit shape (STATE_PERSISTS). The producer keeps a direct-
    # Telegram fallback, so a broken admin package can't silence a finding.
    TunableKey(
        path="alerts.security_cve_scan.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.security_cve_scan.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Push daily CVE-scan findings (newly-published vulnerabilities affecting OpenClaw / macOS).",
    ),
    TunableKey(
        path="alerts.security_cve_scan.cooldown_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.security_cve_scan.cooldown_seconds",
        strength=Strength.FREE,
        stock_default=86400,
        type_hint="int",
        description=(
            "Minimum seconds between repeat pushes of the same CVE finding-set. "
            "Default 24h: an unpatched finding announced once per day, not on "
            "every scan."
        ),
    ),
    # Per-source cooldowns — defaults reflect 'how often is this finding
    # genuinely worth re-paging the operator', not the producer's scan
    # cadence. See spec § "Frequency / cooldown".
    TunableKey(
        path="alerts.audit.cooldown_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.audit.cooldown_seconds",
        strength=Strength.FREE,
        stock_default=86400,
        type_hint="int",
        description=(
            "Minimum seconds between repeat pushes of the same audit finding. "
            "Default 24h: an unresolved finding announced once per day, not "
            "every 15-min scan."
        ),
    ),
    TunableKey(
        path="alerts.pod_report.cooldown_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.pod_report.cooldown_seconds",
        strength=Strength.FREE,
        stock_default=0,
        type_hint="int",
        description="Cooldown for pod-report pushes. Default 0 (daily cadence is natural).",
    ),
    TunableKey(
        path="alerts.spend_alert.cooldown_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.spend_alert.cooldown_seconds",
        strength=Strength.FREE,
        stock_default=86400,
        type_hint="int",
        description="Per-bot-per-day spend-alert cooldown. Default 24h.",
    ),
    TunableKey(
        path="alerts.cron_alert.cooldown_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.cron_alert.cooldown_seconds",
        strength=Strength.FREE,
        stock_default=86400,
        type_hint="int",
        description="Per-cron stalled-alert cooldown. Default 24h.",
    ),
    TunableKey(
        path="alerts.forge_engine.cooldown_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.forge_engine.cooldown_seconds",
        strength=Strength.FREE,
        stock_default=0,
        type_hint="int",
        description="Cooldown for forge-engine job notifications. Default 0 (jobs are unique).",
    ),
    TunableKey(
        path="alerts.signal_notifier.cooldown_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.signal_notifier.cooldown_seconds",
        strength=Strength.FREE,
        stock_default=600,
        type_hint="int",
        description=(
            "Per-signature flap-suppression cooldown for the Signal-store "
            "notifier. Default 10 min, matching Security_bot's gateway-liveness rule."
        ),
    ),
    TunableKey(
        path="alerts.signal_notifier.flap_window_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.signal_notifier.flap_window_seconds",
        strength=Strength.FREE,
        stock_default=1800,
        type_hint="int",
        description=(
            "After a recovery is announced, suppress chat pushes for a "
            "fresh fire of the same signature within this window. Default "
            "30 min, matching the observed flap cadence on Node-25-degraded "
            "gateways (signal_notifier investigation 2026-05-21). Set to 0 "
            "to disable suppression — every transition pages."
        ),
    ),
    # Per-severity delivery grace — the operator's "time threshold for
    # issues" (spec-delta-transient-delivery-grace-2026-06-26, L3). A firing
    # Signal younger than its severity's grace is held back; if it resolves
    # before the grace elapses it never pages (signal_notifier) and is a
    # digest cancellation candidate (digest_dispatcher L1). Distinct from
    # flap_window_seconds (which gates a re-fire, not a first fire). The
    # ``alert`` entry is ALWAYS clamped to 0 in code — a critical is never
    # delayed regardless of what's set here. stock_default mirrors
    # signal_notifier/grace.py::_DEFAULT_GRACE_SECONDS_BY_SEVERITY.
    TunableKey(
        path="alerts.grace_seconds_by_severity",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.grace_seconds_by_severity",
        strength=Strength.FREE,
        stock_default={"alert": 0, "warn": 900, "info": 900},
        type_hint="dict",
        description=(
            "Per-severity grace window (seconds) before a firing Signal "
            "reaches the operator. A transient that self-resolves inside its "
            "severity's grace is never pushed and is elided from the digest. "
            "'alert' is clamped to 0 in code (a critical is never delayed) "
            "regardless of this value; tune 'warn'/'info' to set how long a "
            "blip must persist before it pages. Default warn/info 15 min."
        ),
    ),
    # signal_notifier producer deny-list — producers explicitly EXCLUDED
    # from chat routing because they already call dispatcher.send() on their
    # own path (direct-dispatch). Routing them through signal_notifier as well
    # would double-message the operator on every event.
    #
    # Model: all producers are loud by default. Only this small deny-list is
    # excluded. A brand-new monitor that writes Signals reaches operator chat
    # automatically — no config edit or allowlist entry needed.
    #
    # This replaces the previous alerts.signal_notifier.producers allowlist
    # (removed 2026-06-09). The single source of truth is
    # signal_notifier._DIRECT_DISPATCH_PRODUCERS; this stock_default must
    # stay in sync with that frozenset.
    TunableKey(
        path="alerts.signal_notifier.excluded_producers",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.signal_notifier.excluded_producers",
        strength=Strength.FREE,
        stock_default=[
            # Both call dispatcher.send(catalog_event="cost.*") directly.
            # Adding them here would produce two cost-alert messages per event.
            "cost_watchdog",
            "spend_alert",
        ],
        type_hint="list",
        description=(
            "Signal producers excluded from signal_notifier chat routing. "
            "Only direct-dispatch producers (those that already call "
            "dispatcher.send on their own path) should appear here. All other "
            "producers are loud by default — add to this list to silence a "
            "specific producer in chat (the Alerts page still shows its Signals)."
        ),
    ),
    # ── standing-alerts inventory digest ──────────────────────────────
    # NOT a dispatcher source: standing_alerts renders a section INTO the
    # daily pod report and is delivered by pod_report's own
    # summaries.daily_pod_report dispatch. There is deliberately no
    # ``_DEFAULT_SOURCE_ENABLED["standing_alerts"]`` entry — a second
    # dispatch path would mean a second daily message.
    #
    # Why the feature exists: signal_notifier announces transitions only,
    # and its cold-start guard permanently silences whatever backlog was
    # firing at first run (measured 2026-07-29: 95/99 firing Signals on the
    # mini, 34/36 on the Linux VPS had never been announced). These keys
    # tune the inventory channel that covers the gap.
    TunableKey(
        path="alerts.standing_alerts.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.standing_alerts.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description=(
            "Include the standing-alerts summary in the daily pod report — "
            "how many alerts are still open, how many you were never told "
            "about, and the worst few with their fixes. Turn off to keep the "
            "report to today's news only (the Alerts page still lists them)."
        ),
    ),
    TunableKey(
        path="alerts.standing_alerts.min_interval_hours",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.standing_alerts.min_interval_hours",
        strength=Strength.FREE,
        stock_default=24,
        type_hint="int",
        description=(
            "Minimum hours between standing-alerts summaries. The pod-report "
            "job ticks hourly; this is what keeps the summary to once a day. "
            "0 means include it in every pod report."
        ),
    ),
    TunableKey(
        path="alerts.standing_alerts.top_n",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.standing_alerts.top_n",
        strength=Strength.FREE,
        stock_default=3,
        type_hint="int",
        description=(
            "How many of the worst standing alerts to list by name in the "
            "summary (worst severity first, then oldest). The rest are "
            "counted, not listed, so the message stays phone-readable."
        ),
    ),
    # ── alert_rate_breaker source (the breaker's own storm notice) ─────
    # Registered so the drift test (schema ↔ dispatcher defaults) passes
    # and the operator gets a mute toggle for the storm notice. Default-ON
    # + cooldown 0 (PER_EVENT_UNIQUE) — the breaker owns "exactly once +
    # hourly heartbeat" itself.
    TunableKey(
        path="alerts.alert_rate_breaker.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.alert_rate_breaker.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description=(
            "Push the global rate breaker's '🌊 Alert storm' notice when "
            "Evolve starts batching a flood of alerts into the digest. "
            "Default-on so the operator learns WHY their phone went quiet."
        ),
    ),
    TunableKey(
        path="alerts.alert_rate_breaker.cooldown_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.alert_rate_breaker.cooldown_seconds",
        strength=Strength.FREE,
        stock_default=0,
        type_hint="int",
        description=(
            "Per-dedup-key cooldown for the storm notice. 0: the breaker "
            "controls cadence itself (one notice per episode + hourly "
            "heartbeat) and passes dedup_key=None, so this is a no-op floor."
        ),
    ),
    # ── alerts.rate_cap.* — global outbound circuit breaker ────────────
    # Producer-agnostic backstop (alerts/rate_breaker.py). A ceiling at the
    # single send chokepoint so no producer — misbehaving, new, or
    # misconfigured — can firehose the operator's chat. Defaults mirror
    # rate_breaker.py's compiled-in constants.
    TunableKey(
        path="alerts.rate_cap.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.rate_cap.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description=(
            "Master switch for the global outbound rate breaker. When on, "
            "immediate sends per channel are capped per rolling window; "
            "over-cap alerts roll into the daily digest (never dropped)."
        ),
    ),
    TunableKey(
        path="alerts.rate_cap.window_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.rate_cap.window_seconds",
        strength=Strength.FREE,
        stock_default=3600,
        type_hint="int",
        description="Rolling window (seconds) the rate cap + storm thresholds measure over. Default 1h.",
    ),
    TunableKey(
        path="alerts.rate_cap.max_per_window",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.rate_cap.max_per_window",
        strength=Strength.FREE,
        stock_default=10,
        type_hint="int",
        description=(
            "Max immediate (non-critical) sends per channel per window. "
            "Past this, alerts batch into the daily digest. Default 10/hour."
        ),
    ),
    TunableKey(
        path="alerts.rate_cap.critical_max_per_window",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.rate_cap.critical_max_per_window",
        strength=Strength.FREE,
        stock_default=30,
        type_hint="int",
        description=(
            "Secondary ceiling for critical / safety-critical alerts that "
            "bypass the normal cap. Higher, but finite, so even a misfiring "
            "'critical' producer can't firehose. Default 30/hour."
        ),
    ),
    TunableKey(
        path="alerts.rate_cap.storm_trip_count",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.rate_cap.storm_trip_count",
        strength=Strength.FREE,
        stock_default=20,
        type_hint="int",
        description=(
            "Arrivals per channel per window that trip full digest-only "
            "storm mode (heartbeat + non-critical alerts batched). Default 20."
        ),
    ),
    TunableKey(
        path="alerts.rate_cap.storm_low_water",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.rate_cap.storm_low_water",
        strength=Strength.FREE,
        stock_default=5,
        type_hint="int",
        description=(
            "Arrivals per window below which a storm episode auto-resets. "
            "The gap to storm_trip_count is the hysteresis band that "
            "prevents flapping in/out of storm mode. Default 5."
        ),
    ),
    TunableKey(
        path="alerts.rate_cap.storm_heartbeat_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.rate_cap.storm_heartbeat_seconds",
        strength=Strength.FREE,
        stock_default=3600,
        type_hint="int",
        description="How often to re-emit the 'still in storm mode' heartbeat. Default hourly.",
    ),
    # ── alerts.flap_demote.* — recurring-flapper demotion ──────────────
    # The multi-hour-flap layer ABOVE the per-severity grace (alerts/flapper.py).
    # A signature that oscillates K+ times in a 24h window — each cycle's
    # lifetime exceeding the grace and each re-fire landing outside the flap
    # window, so neither single-transient layer catches it — is auto-demoted to
    # the daily digest until it stabilizes for a cooldown, then self-lifts.
    # Defaults mirror flapper.py's compiled-in constants.
    TunableKey(
        path="alerts.flap_demote.enabled",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.flap_demote.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description=(
            "Master switch for recurring-flapper demotion. When on, a Signal "
            "signature that fires-and-clears several times a day is routed to "
            "the daily digest (its standalone clears suppressed) instead of "
            "paging on every cycle, until it goes quiet for the cooldown. "
            "Critical (alert-severity) conditions are never demoted."
        ),
    ),
    TunableKey(
        path="alerts.flap_demote.count",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.flap_demote.count",
        strength=Strength.FREE,
        stock_default=4,
        type_hint="int",
        description=(
            "Oscillations (distinct fires) within the window that trip "
            "demotion to the digest. Default 4 — catches the few-times-a-day "
            "flap while leaving a genuine fire-clear-fire blip (≤3 cycles) "
            "paging normally."
        ),
    ),
    TunableKey(
        path="alerts.flap_demote.window_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.flap_demote.window_seconds",
        strength=Strength.FREE,
        stock_default=86400,
        type_hint="int",
        description=(
            "Rolling window the oscillation count is measured over. "
            "Default 24h — the accounting period for a 'few times a day' flap."
        ),
    ),
    TunableKey(
        path="alerts.flap_demote.cooldown_seconds",
        store=Store.BETTER_ENGINE,
        target_path="better-engine-config.json::pod_defaults.alerts.flap_demote.cooldown_seconds",
        strength=Strength.FREE,
        stock_default=21600,
        type_hint="int",
        description=(
            "How long a demoted signature must go with NO new fire before the "
            "demotion self-lifts and it pages normally again. Default 6h "
            "(≈3× the motivating ~2h flap cadence)."
        ),
    ),
)




# Bot-side openclaw.json — evolve-plugin Policy keys + select agent defaults.
#
# Target_path strings now match the real on-disk JSON layout:
# ``plugins.entries.<plugin>.config.<key>`` (note ``.entries.<plugin>.config.``
# segments that were missing pre-2026-05-25). See
# docs/spec-openclaw-json-derived-artifact-2026-05-24.md §6.
#
# Phase 3 narrowly materializes the ``plugins.entries.evolve.config.*``
# slice — that's where the strict ``additionalProperties: false`` plugin
# schema lives and where the PR #1525 incident originated. Other
# openclaw.json keys (gateway.*, session.*, channels.*, the rest of
# agents.defaults.*) remain managed by the legacy repair passes in
# ``deploy.ensure_plugin_config`` for now; a follow-up PR widens the
# materialized slice and retires those passes.
_OPENCLAW: tuple[TunableKey, ...] = (
    # ── plugins.entries.evolve.config.* (strict-schema block) ───────────────
    TunableKey(
        path="openclaw.plugins.evolve.tier",
        store=Store.OPENCLAW,
        target_path="openclaw.json::plugins.entries.evolve.config.tier",
        strength=Strength.FREE,
        stock_default="full",
        type_hint="enum",
        description="Per-bot Evolve integration tier (off / monitor / manage / full).",
        per_bot=True,
    ),
    TunableKey(
        path="openclaw.plugins.evolve.summarizerMinTurns",
        store=Store.OPENCLAW,
        target_path="openclaw.json::plugins.entries.evolve.config.summarizerMinTurns",
        strength=Strength.SHIPPED_POLICY,
        stock_default=2,
        type_hint="int",
        description="Skip per-session LLM outcome extraction below this turn count.",
        per_bot=True,
    ),
    TunableKey(
        path="openclaw.plugins.evolve.classifierKeywordConfidenceFloor",
        store=Store.OPENCLAW,
        target_path="openclaw.json::plugins.entries.evolve.config.classifierKeywordConfidenceFloor",
        strength=Strength.SHIPPED_POLICY,
        stock_default=0.80,
        type_hint="float",
        description=(
            "Keyword-classifier confidence above which we skip the LLM tier "
            "classifier. Tuned from telemetry."
        ),
        per_bot=True,
    ),
    TunableKey(
        path="openclaw.plugins.evolve.costLedgerEnabled",
        store=Store.OPENCLAW,
        target_path="openclaw.json::plugins.entries.evolve.config.costLedgerEnabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Append cost_event records to annotations JSONL on every llm_output.",
        per_bot=True,
    ),
    TunableKey(
        path="openclaw.plugins.evolve.tierClassification",
        store=Store.OPENCLAW,
        target_path="openclaw.json::plugins.entries.evolve.config.tierClassification",
        strength=Strength.ADVISORY,
        stock_default="session",
        type_hint="enum",
        description="Classify at session or turn granularity.",
        per_bot=True,
    ),
    TunableKey(
        path="openclaw.plugins.evolve.dashboardEnabled",
        store=Store.OPENCLAW,
        target_path="openclaw.json::plugins.entries.evolve.config.dashboardEnabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Per-bot dashboard surfacing on/off.",
        per_bot=True,
    ),
    TunableKey(
        path="openclaw.plugins.evolve.enableLLMSummarization",
        store=Store.OPENCLAW,
        target_path="openclaw.json::plugins.entries.evolve.config.enableLLMSummarization",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Use the LLM to extract session outcome summaries.",
        per_bot=True,
    ),
    TunableKey(
        path="openclaw.plugins.evolve.enableLLMExtraction",
        store=Store.OPENCLAW,
        target_path="openclaw.json::plugins.entries.evolve.config.enableLLMExtraction",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Use the LLM to extract structured session signals.",
        per_bot=True,
    ),
    TunableKey(
        path="openclaw.plugins.evolve.enableTaskExtraction",
        store=Store.OPENCLAW,
        target_path="openclaw.json::plugins.entries.evolve.config.enableTaskExtraction",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Run task_extractor at session end.",
        per_bot=True,
    ),
    TunableKey(
        path="openclaw.plugins.evolve.hooks.allowConversationAccess",
        store=Store.OPENCLAW,
        target_path="openclaw.json::plugins.entries.evolve.hooks.allowConversationAccess",
        strength=Strength.FREE,
        stock_default=False,
        type_hint="bool",
        description=(
            "Per-bot opt-in for agent_end / llm_output hook access. Note: lives "
            "under plugins.entries.evolve.hooks (NOT under .config)."
        ),
        per_bot=True,
    ),
    # ── agents.defaults.* (Policy keys outside the evolve plugin) ──────────
    # Added 2026-05-25 to make Security_bot's haiku-pin workaround representable.
    # See project_cost_alerting_blackout_2026_05_20 memory.
    #
    # Phase 3d (PR #1545+1) flipped the policy-slice materializer on for
    # these entries — ``materialize_openclaw_policy_slice`` applies any
    # override here to openclaw.json's ``agents.defaults.model.primary``
    # path on the next deploy. The earlier "Phase 3 doesn't yet materialize"
    # comment is obsolete; the only caveat now is that this applier (the
    # L2 ``UpdatePermissionConfigApplier``) is scoped to
    # ``tools.*`` / ``commands.*`` so the model pin still has to land via
    # the customizations UI / operator-edit path, not via RSI proposal.
    TunableKey(
        path="openclaw.agents.defaults.model.primary",
        store=Store.OPENCLAW,
        target_path="openclaw.json::agents.defaults.model.primary",
        strength=Strength.FREE,
        stock_default=None,
        type_hint="string",
        description=(
            "Primary model for the bot's main agent. Stock default is None "
            "(auto-detected from auth-profiles at deploy time). Override only "
            "to pin a specific model (e.g. Security_bot's haiku-4-5 workaround for "
            "OC#84825)."
        ),
        per_bot=True,
    ),
    # ── Anthropic prompt-cache retention ────────────────────────────────────
    # OpenClaw's per-model ``params.cacheRetention`` knob picks the TTL for
    # Anthropic prompt-cache writes: "short" = 5 minutes (Anthropic's
    # default, 1x cache-write price); "long" = 1 hour (~2x cache-write
    # price, but eliminates TTL invalidations on conversational turns
    # spaced more than 5 minutes apart).
    #
    # Surfaced as a single logical key with a wildcard target_path because
    # the operator almost always wants a uniform policy across all the
    # Anthropic models in the bot's catalog (Sonnet, Opus, Haiku); the
    # materializer fans it out across every entry under
    # ``agents.defaults.models.<id>.params`` whose provider is Anthropic.
    # Non-Anthropic providers don't have this concept and are untouched.
    #
    # Motivating incident: team-bot-a session 3d5cde22 (2026-05-29 Slack DM)
    # cost $7.67, 92% from prompt-cache writes. Root cause was team-bot-a's
    # ``cacheRetention="short"`` on Sonnet/Opus/Haiku/Sonnet-4-5 combined
    # with the human-paced Slack cadence (>5min gaps between user turns)
    # which TTL'd the cache before each reuse. Flipping to "long" would
    # have eliminated ~7 invalidations on that one session.
    TunableKey(
        path="openclaw.agents.defaults.models.cacheRetention",
        store=Store.OPENCLAW,
        target_path="openclaw.json::agents.defaults.models.*.params.cacheRetention",
        strength=Strength.FREE,
        stock_default="short",
        type_hint="enum",
        description=(
            "Anthropic prompt-cache TTL applied to every Anthropic model in "
            "the bot's catalog. 'short' (5-minute TTL, OpenClaw default) is "
            "cheapest per cache write but invalidates on conversational gaps. "
            "'long' (1-hour TTL) doubles cache-write price but eliminates "
            "TTL invalidations for human-paced chats like Slack DMs."
        ),
        per_bot=True,
    ),
    # ── Per-session cost ceiling (PR C — runaway-session sentinel) ─────────
    # Complementary to ``better_engine.budget.per_bot_daily_hard_usd``: the
    # daily cap is per-bot per-day across all sessions and trips at midnight,
    # this is per-session and trips mid-conversation. A 33-turn, $7.67
    # session over 2 hours (motivating incident: team-bot-a session
    # 3d5cde22, 2026-05-29) is only ~38% of a $20 daily cap but is exactly
    # the kind of single-session runaway we want to catch in flight.
    #
    # Opt-in: stock_default=None means "no cap" — operators who never set
    # the knob see no behavioral change. When set to a positive float, the
    # plugin's SessionCostMonitor accumulates per-session spend on each
    # llm_output event and writes a per-session breaker file when the cap
    # is crossed; the next turn on that session (user or auto-source) is
    # rejected with a budget-exceeded message until the operator raises
    # the cap or clears the breaker.
    #
    # NOT auto-applied by any generator today. The dotpath is whitelisted
    # by the UpdateAgentDefaults L2 applier so a future generator could
    # tune it autonomously once telemetry shows safe defaults, but PR C
    # ships only the operator surface.
    TunableKey(
        path="openclaw.agents.defaults.sessionBudgetCapUsd",
        store=Store.OPENCLAW,
        target_path="openclaw.json::agents.defaults.sessionBudgetCapUsd",
        strength=Strength.FREE,
        stock_default=None,
        type_hint="float",
        description=(
            "Per-session cost ceiling in USD. When a single session "
            "exceeds this, the next turn is rejected with a "
            "budget-exceeded message until the operator raises the cap "
            "or resets the session breaker. Complements per-bot daily "
            "caps — this sentinel catches single-session runaways before "
            "the next-day sweep. Null (default) = no cap."
        ),
        per_bot=True,
    ),
    # ── Permission-posture fields (Phase 3d) ────────────────────────────────
    # Set of fields the L2 ``UpdatePermissionConfig`` applier mutates.
    # Spec: docs/spec-openclaw-json-derived-artifact-2026-05-24.md §7 +
    # docs/spec-permission-posture-2026-05-10.md.
    #
    # These are NOT under ``plugins.entries.evolve.config.*`` — they're
    # top-level openclaw.json keys (``tools.*`` and ``commands.*``).
    # The materializer's ``materialize_openclaw_policy_slice`` walks these
    # entries; overrides supersede the runtime inference in
    # ``deploy._infer_exec_policy`` and ``deploy._apply_permission_baseline_gap_fills``.
    #
    # ``stock_default`` here is informational (the value an install would
    # get if nothing overrode it); the materializer does NOT auto-promote
    # drift on these fields because their "default" is inferred per-bot
    # at deploy time (e.g. exec.security depends on exec-approvals.json
    # contents). Auth-drift detection is handled by the auth_drift_filler
    # generator, not the materializer.
    TunableKey(
        path="openclaw.tools.exec.security",
        store=Store.OPENCLAW,
        target_path="openclaw.json::tools.exec.security",
        strength=Strength.ADVISORY,
        stock_default="deny",
        type_hint="enum",
        description=(
            "Exec security mode (deny / allowlist / full). Deploy infers "
            "from exec-approvals.json content unless overridden here."
        ),
        per_bot=True,
    ),
    TunableKey(
        path="openclaw.tools.exec.ask",
        store=Store.OPENCLAW,
        target_path="openclaw.json::tools.exec.ask",
        strength=Strength.ADVISORY,
        stock_default="on-miss",
        type_hint="enum",
        description=(
            "Exec approval prompt mode. ``off`` is refused by the applier — "
            "it removes the approval gate entirely."
        ),
        per_bot=True,
    ),
    TunableKey(
        path="openclaw.tools.exec.host",
        store=Store.OPENCLAW,
        target_path="openclaw.json::tools.exec.host",
        strength=Strength.ADVISORY,
        stock_default="none",
        type_hint="string",
        description="Per-bot host-exec scope (no shipped default; per-bot specific).",
        per_bot=True,
    ),
    TunableKey(
        path="openclaw.tools.fs.workspaceOnly",
        store=Store.OPENCLAW,
        target_path="openclaw.json::tools.fs.workspaceOnly",
        strength=Strength.ADVISORY,
        stock_default=True,
        type_hint="bool",
        description="Restrict fs tools to workspace dir.",
        per_bot=True,
    ),
    TunableKey(
        path="openclaw.tools.web.search.enabled",
        store=Store.OPENCLAW,
        target_path="openclaw.json::tools.web.search.enabled",
        strength=Strength.ADVISORY,
        stock_default=True,
        type_hint="bool",
        description="Enable the web.search tool.",
        per_bot=True,
    ),
    TunableKey(
        path="openclaw.tools.web.fetch.enabled",
        store=Store.OPENCLAW,
        target_path="openclaw.json::tools.web.fetch.enabled",
        strength=Strength.ADVISORY,
        stock_default=True,
        type_hint="bool",
        description="Enable the web.fetch tool.",
        per_bot=True,
    ),
    TunableKey(
        path="openclaw.commands.native",
        store=Store.OPENCLAW,
        target_path="openclaw.json::commands.native",
        strength=Strength.ADVISORY,
        stock_default="auto",
        type_hint="enum",
        description="Native-command discovery (auto / off / explicit list).",
        per_bot=True,
    ),
    TunableKey(
        path="openclaw.commands.nativeSkills",
        store=Store.OPENCLAW,
        target_path="openclaw.json::commands.nativeSkills",
        strength=Strength.ADVISORY,
        stock_default="auto",
        type_hint="enum",
        description="Native-skills discovery (auto / off / explicit list).",
        per_bot=True,
    ),
    TunableKey(
        path="openclaw.commands.elevated",
        store=Store.OPENCLAW,
        target_path="openclaw.json::commands.elevated",
        strength=Strength.ADVISORY,
        stock_default="deny",
        type_hint="enum",
        description="Elevated-command policy (deny / allowlist / ask).",
        per_bot=True,
    ),
    TunableKey(
        path="openclaw.commands.useAccessGroups",
        store=Store.OPENCLAW,
        target_path="openclaw.json::commands.useAccessGroups",
        strength=Strength.ADVISORY,
        stock_default=False,
        type_hint="bool",
        description="Honor command access-group declarations.",
        per_bot=True,
    ),
    TunableKey(
        path="openclaw.commands.ownerAllowFrom",
        store=Store.OPENCLAW,
        target_path="openclaw.json::commands.ownerAllowFrom",
        strength=Strength.ADVISORY,
        stock_default=[],
        type_hint="list",
        description="Bot IDs allowed to inject owner-commands into this bot.",
        per_bot=True,
    ),
)


_EVOLVE_TIERS: tuple[TunableKey, ...] = (
    TunableKey(
        path="evolve_tiers.routing.maintenanceTier",
        store=Store.EVOLVE_TIERS,
        target_path="evolve-tiers.json::routing.maintenanceTier",
        strength=Strength.FREE,
        stock_default=None,
        type_hint="enum",
        description="Tier for maintenance-class sessions (per-bot).",
        per_bot=True,
    ),
    TunableKey(
        path="evolve_tiers.routing.backgroundTier",
        store=Store.EVOLVE_TIERS,
        target_path="evolve-tiers.json::routing.backgroundTier",
        strength=Strength.FREE,
        stock_default=None,
        type_hint="enum",
        description="Tier for background-class sessions (per-bot).",
        per_bot=True,
    ),
    TunableKey(
        path="evolve_tiers.routing.ambiguousTier",
        store=Store.EVOLVE_TIERS,
        target_path="evolve-tiers.json::routing.ambiguousTier",
        strength=Strength.FREE,
        stock_default=None,
        type_hint="enum",
        description="Tier for ambiguous sessions (per-bot).",
        per_bot=True,
    ),
    TunableKey(
        path="evolve_tiers.routing.enabled",
        store=Store.EVOLVE_TIERS,
        target_path="evolve-tiers.json::routing.enabled",
        strength=Strength.FREE,
        stock_default=True,
        type_hint="bool",
        description="Tier routing on/off (per-bot).",
        per_bot=True,
    ),
    TunableKey(
        path="evolve_tiers.routing.confidenceThreshold",
        store=Store.EVOLVE_TIERS,
        target_path="evolve-tiers.json::routing.confidenceThreshold",
        strength=Strength.SHIPPED_POLICY,
        stock_default=0.80,
        type_hint="float",
        description=(
            "Classifier confidence above which routing applies. Tuned from "
            "telemetry."
        ),
        per_bot=True,
    ),
)


# Identity-category prose / whole-document keys. These appear in the schema
# so the customizations UI can list them, but their cascade is structurally
# trivial: there's no shipped default.
_IDENTITY_DOCS: tuple[TunableKey, ...] = (
    TunableKey(
        path="bot_guide.<bot_id>",
        store=Store.BOT_GUIDE,
        target_path="bot_guides/<bot_id>.md",
        strength=Strength.FREE,
        stock_default=None,
        type_hint="doc",
        description="Per-bot tone/voice guidance authored by the operator.",
        per_bot=True,
    ),
    TunableKey(
        path="soul.<bot_id>",
        store=Store.SOUL,
        target_path="SOUL.md",
        strength=Strength.FREE,
        stock_default=None,
        type_hint="doc",
        description="Per-bot SOUL.md (identity / role).",
        per_bot=True,
    ),
    TunableKey(
        path="agents.<bot_id>",
        store=Store.AGENTS,
        target_path="AGENTS.md",
        strength=Strength.FREE,
        stock_default=None,
        type_hint="doc",
        description="Per-bot AGENTS.md (agent instructions).",
        per_bot=True,
    ),
)


SCHEMA: tuple[TunableKey, ...] = (
    *_NETWORK,
    *_BETTER_ENGINE,
    *_ALERTS,
    *_OPENCLAW,
    *_EVOLVE_TIERS,
    *_IDENTITY_DOCS,
)


# ─────────────────────────────────────────────────────────────────────────────
# Lookups
# ─────────────────────────────────────────────────────────────────────────────


def by_path(path: str) -> TunableKey | None:
    """Return the schema entry for ``path`` or None if unknown."""
    for entry in SCHEMA:
        if entry.path == path:
            return entry
    return None


def by_store(store: str) -> tuple[TunableKey, ...]:
    """Return all schema entries for a given backing store."""
    return tuple(e for e in SCHEMA if e.store == store)
