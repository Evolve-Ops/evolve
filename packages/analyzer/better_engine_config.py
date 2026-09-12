"""better_engine_config — Pod + per-bot toggles for Better Engine.

Spec: docs/archive/specs/spec-rsi-layer-1-foundation-2026-04-18.md §10.

Single file: ``{shared_dir}/better-engine-config.json``. Pod defaults at
the top level, per-bot overrides under the ``bots.<bot_id>`` key.

Resolution rule:
    resolved(bot, key) = bots[bot][key] if present else pod_defaults[key]

If neither is present, fall back to a compiled-in default.

L1 fields:
    better_engine.enabled: bool (default True)
    rsi.enabled: bool (default True)

Per-bot override keys mirror the pod-level shape but only for fields a
bot-specific setting makes sense for. L1 supports both flags at both scopes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


CONFIG_SCHEMA_VERSION = 1
DEFAULT_CONFIG_FILENAME = "better-engine-config.json"

# On-disk mode for the config file. This holds budgets and feature toggles —
# no tokens — and is deliberately readable by the separate ``evo`` macOS user
# (``evolve_admin/evo/tools/pod_state_cost_caps.py`` reads it from the evo
# gateway subprocess). ``save()`` pins it explicitly: tempfile+rename would
# otherwise carry mkstemp's 0600 onto the destination and lock evo out.
DEFAULT_CONFIG_MODE = 0o644


# ─────────────────────────────────────────────────────────────────────────────
# Compiled-in defaults (what ships if the config file is missing a key)
# ─────────────────────────────────────────────────────────────────────────────

_COMPILED_DEFAULTS: dict[str, Any] = {
    "better_engine": {
        "enabled": True,
    },
    "rsi": {
        "enabled": True,
    },
    # Budget cap defaults — conservative to force explicit operator
    # configuration; Budget Hawk vetoes above hard cap.
    #
    # The "Bot setup" UI lets the operator set a single concrete number per
    # bot — ``per_bot_monthly_cap_usd``. When set, the daily warn/hard caps
    # are *derived* from it (warn ≈ 1.5× of the daily average, hard ≈ 2.5×).
    # When unset, fall back to the explicit per_bot_daily_* defaults.
    "budget": {
        # Pod-wide monthly cap (legacy). Retained for back-compat with
        # the spend_caps.py global-cap path; per-bot caps below take
        # precedence per the resolution rules in
        # ``budget_warn_cap_usd`` / ``budget_hard_cap_usd``.
        "monthly_cap_usd": 50.00,
        # Pod-total weekly spend alert (aggregate across all bots). Opt-in;
        # only meaningful at pod_defaults level. Phase 8 will migrate
        # network.json::thresholds.weeklySpendAlertUsd into this slot.
        "pod_weekly_warn_usd": None,
        "per_bot_daily_warn_usd": 2.00,
        "per_bot_daily_hard_usd": 5.00,
        # No default monthly cap per bot — None means "use the per_bot_daily_*
        # values directly". Operators set this via Bot setup.
        "per_bot_monthly_cap_usd": None,
        # Per-session cost ceiling — rejects next turn in any session that
        # crosses the cap. Opt-in (None = no cap); operator-set via Bot
        # setup. Lands in openclaw.json::agents.defaults.sessionBudgetCapUsd
        # via the openclaw materializer.
        "per_bot_session_cost_cap_usd": None,
        # Anthropic prompt-cache TTL: "short" (5-min, OC default) or
        # "long" (1-hr). Pure cost-optimization knob — no enforcement.
        # Opt-in (None = inherit OC default of "short"); operator-set via
        # Bot setup. Lands in openclaw.json::agents.defaults.models.*.params.cacheRetention
        # for every Anthropic model in the catalog.
        "per_bot_cache_retention": None,
        # ── Phase 5 of cost-cap normalization (spec: internal/spec-cost-caps-2026-06-05.md) ──
        # The graduated remediation ladder — three escalating tiers between
        # daily_warn (alert) and the in-flight session sentinel. All opt-in
        # (None = no enforcement at that tier). Ordering invariant:
        # ``l2_breaker > l1_breaker > tier_downgrade > daily_warn`` when all
        # four are set; partial sets are valid (any missing rung is just
        # "no enforcement at that tier").
        "tier_downgrade_usd": None,
        "l2_breaker_usd": None,
        # Rolling 7-day per-bot warn cap. Fires a Telegram alert on cross,
        # deduped once per ISO week. Distinct from the existing pod-wide
        # weekly threshold (network.json::thresholds.weeklySpendAlertUsd,
        # migrating to ``pod_weekly_warn_usd`` at pod_defaults level in
        # Phase 8). Opt-in.
        "weekly_warn_usd": None,
    },
    # Slice 5b8 — conversational approval (rec_pending wizard phase).
    # Spec: internal/spec-better-engine-conversational-approval-2026-04-18.md
    "conversational_approval": {
        # Top-level switch. Currently only gates the push_preamble
        # delivery path (see ``push_preamble_enabled``); the rec_pending
        # / wizard dispatch is always on.
        "enabled": True,
        # Stage-2 LLM intent parser switch. When False, only the
        # deterministic stage-1 keyword classifier runs; replies that
        # miss stage 1 fall straight through to the clarify path.
        # Saves Haiku calls in cost-sensitive deployments.
        "llm_intent_parse_enabled": True,
        # Below this confidence the engine treats the result as
        # ambiguous and re-renders with the clarify variant.
        "confidence_threshold": 0.80,
        # Default snooze period when the user said "snooze" without a
        # duration hint. Stage-2 parsed durations override per call.
        "default_snooze_days": 3,
        # Max idle minutes a pending rec_pending session lingers
        # before the engine treats it as expired and the bot resumes
        # normal behavior. The pending rec stays in the better-engine
        # queue — the user can re-invoke `evo` to see it again. Default
        # is conversational (1 hour): if you walked away for an hour,
        # your next message is a fresh thought, not a delayed vote on
        # the earlier pitch. Days-scale values caused stale sessions to
        # hijack unrelated later messages (e.g. a 5-day-old "brain
        # teaser" pitch interpreting a health-issue message as a vote).
        "pending_expiry_minutes": 60,
        # Push-mode preamble: when an urgent rec is queued and the
        # user starts a turn unrelated to evo, the engine can prepend
        # a "before we dive in — I noticed X" preamble. Off by default
        # because push messaging needs careful rate-limiting.
        "push_preamble_enabled": False,
    },
}


# Daily-cap derivation factors when per_bot_monthly_cap_usd is set.
# A 30-day month, with peak-day allowance: warn at 1.5× average daily,
# hard cap at 2.5× average daily.
_DAYS_PER_MONTH = 30.0
_WARN_MULTIPLIER = 1.5
_HARD_MULTIPLIER = 2.5


# ─────────────────────────────────────────────────────────────────────────────
# Graduated new-bot daily hard cap (product default — ships in code, NOT a
# per-pod proposal). A freshly-created bot gets extra activation-window
# headroom (``NEW_BOT_DAILY_HARD_USD`` for its first ``NEW_BOT_GRADUATION_DAYS``
# days), then falls to the pod default. The layer sits strictly under any
# explicit per-bot override:
#
#     explicit per-bot override  >  graduated new-bot default  >  pod default
#
# Why: a fresh wizard bot (``ledger``) spent $30.26 in its first two days with
# no per-bot cap and only a pod default — the designed cap backstop was
# effectively absent on new bots, so the warn alert fired but nothing stopped
# the spend. Mature bots here run ~$1–2/day, so the $5 mature default is
# already 2.5–5× normal; the first-7-days $10 leaves activation headroom.
# Finding: internal/finding-new-bot-activation-cost-2026-06-12.md.
#
# The window-day count and the activation cap are intentionally compiled
# constants, not config keys: this is a product default, so a fresh-install
# pod must get it with zero proposals and zero per-pod config writes. The
# bot's creation timestamp travels with the config (``bots.<bot>.created_at``),
# stamped by ``deploy.add_bot`` at the single bot-creation entry point.
NEW_BOT_GRADUATION_DAYS = 7
NEW_BOT_DAILY_HARD_USD = 10.0


# ─────────────────────────────────────────────────────────────────────────────
# One-time provisioning (creation-budget) ceiling (product default — ships in
# code, NOT a per-pod proposal). A bot's *cumulative* spend during its
# activation window (the day-0/day-1 forge build + first-audit window) is
# bounded by this ceiling: once it is crossed, further PROVISIONING dispatches
# (forge install builds + first audits) are refused/paused. Distinct from the
# graduated *daily* cap above — that resets each day; this is a single
# cumulative bound on the whole standup.
#
# Why a cumulative ceiling and not just the daily cap: the `ledger` bot spent
# $30.26 across two days entirely on building + auditing its own starter apps.
# The daily cap tripped on both days but could not govern the spend — forge
# builds and the audit pipeline run via admin-side daemons, outside the L1
# cost-breaker's heartbeat-disable scope, and operator-confirmed installs are
# deliberately exempt from the daily cap. A per-day cap, at any value, can't
# bound a multi-day standup; the cumulative ceiling can.
# Finding: internal/finding-new-bot-activation-cost-2026-06-12.md (decision B).
#
# Like the graduated daily cap, the value + the window are compiled constants,
# not config keys: a fresh-install pod must get the backstop with zero proposals
# and zero per-pod config writes (product-defaults-in-code). An operator can set
# an explicit per-bot override (``bots.<bot>.budget.provisioning_ceiling_usd``)
# to raise it for a bot that genuinely needs a larger standup; the override only
# ever *raises or lowers* a known-positive value — a stored 0/negative reads as
# "unset" and the product default still applies, so the safety ceiling can't be
# silently disabled with a typo. The activation window reuses
# ``NEW_BOT_GRADUATION_DAYS`` so "new bot" means the same thing across the daily
# cap (A) and this ceiling (B).
NEW_BOT_PROVISIONING_CEILING_USD = 12.0


# ─────────────────────────────────────────────────────────────────────────────
# The remediation ladder: rung name → the ``budget`` field that stores it.
#
# This mapping plus ``BetterEngineConfig.resolve_budget_ladder`` is the SINGLE
# reader every cap consumer must go through — the UI (``GET /api/spend-caps``),
# the enforcer (``spend_alert``), the cost detectors (``cost_watchdog``), and
# the evo read tools. They previously each rolled their own walk of the config
# and disagreed about whether ``pod_defaults.budget`` was a fallback: the UI
# reported the pod-wide $20 hard cap while the enforcer resolved ``None`` for
# any bot without an explicit override and enforced nothing at all. Six bots ran
# uncapped for weeks behind a UI that said they were capped; one spent $33.78 in
# a day against a nominal $20 cap. Add a reader here, never in a caller.
# ─────────────────────────────────────────────────────────────────────────────

BUDGET_LADDER_FIELDS: dict[str, str] = {
    "daily_warn": "per_bot_daily_warn_usd",
    "weekly_warn": "weekly_warn_usd",
    "tier_downgrade": "tier_downgrade_usd",
    # l1_breaker is the spec-renamed view of per_bot_daily_hard_usd; storage
    # is unchanged (see ``per_bot_l1_breaker_usd``).
    "l1_breaker": "per_bot_daily_hard_usd",
    "l2_breaker": "l2_breaker_usd",
    "per_session": "per_bot_session_cost_cap_usd",
}


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class BetterEngineConfig:
    """Loaded Better Engine configuration.

    Access via ``resolve(bot_id, *keys)`` or the narrower helpers like
    ``is_better_engine_enabled(bot_id)``. The raw config dicts are exposed
    for inspection but treat them as read-only — write via
    ``BetterEngineConfig.replace_pod_default`` etc.
    """

    schema_version: int
    pod_defaults: dict[str, Any]
    bots: dict[str, dict[str, Any]] = field(default_factory=dict)

    def resolve(self, bot_id: str | None, *keys: str) -> Any:
        """Walk ``keys`` through bot override, then pod default, then compiled.

        Example:
            cfg.resolve("team_bot_a", "rsi", "enabled") → True
            cfg.resolve(None, "better_engine", "enabled") → True

        Returns the compiled default if the key is unknown at all levels
        (which indicates a caller bug — keys should be in the schema).
        """
        # Try bot override first
        if bot_id is not None:
            bot_config = self.bots.get(bot_id, {})
            value = _walk(bot_config, keys)
            if value is _SENTINEL:
                pass  # fall through to pod_defaults
            else:
                return value

        # Try pod defaults
        value = _walk(self.pod_defaults, keys)
        if value is not _SENTINEL:
            return value

        # Compiled-in default
        compiled = _walk(_COMPILED_DEFAULTS, keys)
        if compiled is _SENTINEL:
            raise KeyError(f"unknown config key path: {'.'.join(keys)}")
        return compiled

    def is_better_engine_enabled(self, bot_id: str) -> bool:
        """Is Better Engine enabled for this bot?"""
        return bool(self.resolve(bot_id, "better_engine", "enabled"))

    def is_rsi_enabled(self, bot_id: str) -> bool:
        """Is the RSI layer enabled for this bot?

        Note: ``rsi.enabled`` is only meaningful when ``better_engine.enabled``
        is also True. If Better Engine is off entirely, RSI is off by
        implication regardless of this value.
        """
        if not self.is_better_engine_enabled(bot_id):
            return False
        return bool(self.resolve(bot_id, "rsi", "enabled"))

    # ── Budget caps (L3) ───────────────────────────────────────────────────

    def budget_warn_cap_usd(self, bot_id: str) -> float:
        """Per-bot daily spend threshold for Budget Hawk warning.

        If a per-bot monthly cap is set, derive warn ≈ 1.5× daily average.
        Otherwise fall back to the explicit ``per_bot_daily_warn_usd``.
        """
        monthly = self._per_bot_monthly_cap_usd_or_none(bot_id)
        if monthly is not None:
            return (monthly / _DAYS_PER_MONTH) * _WARN_MULTIPLIER
        return float(self.resolve(bot_id, "budget", "per_bot_daily_warn_usd"))

    def budget_hard_cap_usd(
        self, bot_id: str, *, now: datetime | None = None
    ) -> float:
        """Per-bot daily spend hard cap — Budget Hawk vetoes above this.

        Resolution precedence (highest first):
          1. explicit per-bot monthly cap → derived hard (≈ 2.5× daily avg)
          2. explicit per-bot daily hard override
             (``bots.<bot>.budget.per_bot_daily_hard_usd`` — presence-based,
             so a stored ``0`` = "uncapped" survives, matching the legacy
             ``resolve()`` semantics)
          3. graduated new-bot default — ``NEW_BOT_DAILY_HARD_USD`` while the
             bot is within its first ``NEW_BOT_GRADUATION_DAYS`` days
          4. pod default / compiled (``per_bot_daily_hard_usd`` → 5.00)

        ``now`` defaults to the wall clock; pass it for deterministic tests
        and to align graduation with a caller's evaluation clock.
        """
        monthly = self._per_bot_monthly_cap_usd_or_none(bot_id)
        if monthly is not None:
            return (monthly / _DAYS_PER_MONTH) * _HARD_MULTIPLIER
        # Explicit per-bot override — presence-based (a stored 0 means the
        # operator explicitly uncapped the bot; don't let graduation re-cap it).
        # A non-numeric stored value is malformed config → treat it as absent
        # and fall through to the graduated / pod default.
        if bot_id is not None:
            explicit = _walk(
                self.bots.get(bot_id, {}), ("budget", "per_bot_daily_hard_usd")
            )
            if isinstance(explicit, (int, float)) and not isinstance(explicit, bool):
                return float(explicit)
        graduated = self.new_bot_graduated_hard_cap_usd(bot_id, now=now)
        if graduated is not None:
            return graduated
        return float(self.resolve(bot_id, "budget", "per_bot_daily_hard_usd"))

    # ── New-bot graduated default ──────────────────────────────────────────

    def bot_created_at(self, bot_id: str | None) -> datetime | None:
        """Bot creation timestamp (``bots.<bot>.created_at``), or ``None``.

        Stamped by ``deploy.add_bot`` at the single bot-creation entry point.
        Drives the age-graded new-bot daily-cap default. Returns a tz-aware
        UTC datetime; a naive stored value is assumed UTC. ``None`` when unset
        or unparseable — existing / pre-feature bots carry no stamp, so they
        get no graduation (correct: they're already mature).
        """
        if bot_id is None:
            return None
        raw = _walk(self.bots.get(bot_id, {}), ("created_at",))
        if raw is _SENTINEL or not raw:
            return None
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def set_bot_created_at(
        self, bot_id: str, value: datetime | str | None
    ) -> None:
        """Set or clear ``bots.<bot>.created_at``. Caller persists with ``save``.

        Accepts a datetime (normalized to UTC ISO8601), an ISO8601 string
        (stored verbatim), or ``None`` to clear the stamp.
        """
        bot = self.bots.setdefault(bot_id, {})
        if value is None:
            bot.pop("created_at", None)
        elif isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            bot["created_at"] = value.astimezone(timezone.utc).isoformat()
        else:
            bot["created_at"] = str(value)

    def new_bot_graduated_hard_cap_usd(
        self, bot_id: str | None, *, now: datetime | None = None
    ) -> float | None:
        """The graduated new-bot daily hard cap, or ``None`` when it doesn't apply.

        Returns ``NEW_BOT_DAILY_HARD_USD`` while the bot is within its first
        ``NEW_BOT_GRADUATION_DAYS`` days; ``None`` otherwise (no creation
        stamp, a future stamp / clock skew, or past the window). This is purely
        the age-gated tier — precedence against explicit overrides and the pod
        default is the caller's job (see ``budget_hard_cap_usd`` and
        ``spend_alert._resolve_per_bot_caps``).
        """
        created = self.bot_created_at(bot_id)
        if created is None:
            return None
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        age = now - created
        if age < timedelta(0):
            return None  # creation stamp is in the future — don't graduate
        if age < timedelta(days=NEW_BOT_GRADUATION_DAYS):
            return NEW_BOT_DAILY_HARD_USD
        return None

    # ── Provisioning (creation-budget) ceiling ─────────────────────────────

    def provisioning_ceiling_usd(self, bot_id: str | None) -> float:
        """The cumulative one-time provisioning ceiling for ``bot_id``.

        Precedence (highest first):
          1. explicit per-bot override (``bots.<bot>.budget.
             provisioning_ceiling_usd``) when stored as a *positive* number;
          2. the compiled product default ``NEW_BOT_PROVISIONING_CEILING_USD``.

        A stored ``0`` / negative / non-numeric override reads as "unset" and
        the product default applies — this is a spend backstop, so it must not
        be possible to disable it by writing ``0`` (the inverse of the
        ``per_bot_daily_hard_usd`` "0 = uncapped" convention, on purpose). An
        operator who wants a larger standup sets an explicit higher value.

        Always returns a positive float — callers never have to handle a
        ``None`` "no ceiling" case for the product backstop.
        """
        override = self._resolve_per_bot_opt_in_float(
            bot_id, "provisioning_ceiling_usd"
        )
        if override is not None:
            return override
        return NEW_BOT_PROVISIONING_CEILING_USD

    def set_per_bot_provisioning_ceiling_usd(
        self, bot_id: str, value: float | None,
    ) -> None:
        """Set or clear an explicit per-bot provisioning ceiling override.

        ``None`` removes the override (the bot falls back to the product
        default). Exposed so a later UI bite can read/write the per-bot value;
        the resolver above is the read path everything else uses.
        """
        self._set_per_bot_opt_in_float(bot_id, "provisioning_ceiling_usd", value)

    def in_provisioning_window(
        self, bot_id: str | None, *, now: datetime | None = None,
    ) -> bool:
        """True iff ``bot_id`` is still inside its activation window.

        The provisioning ceiling only *gates* while the bot is new — once the
        bot graduates (``NEW_BOT_GRADUATION_DAYS`` past ``created_at``) the
        standup is over and later forge work (gallery installs, improvements)
        is steady-state, governed by the daily cap rather than the one-time
        ceiling. A bot with no creation stamp (pre-feature / mature) is never
        in the window — correct, it was never freshly provisioned under this
        feature.
        """
        created = self.bot_created_at(bot_id)
        if created is None:
            return False
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        age = now - created
        return timedelta(0) <= age < timedelta(days=NEW_BOT_GRADUATION_DAYS)

    def new_bot_cost_projection(self) -> dict[str, float]:
        """Projected *cap* envelope for a brand-new bot (age 0, no overrides).

        The BE-config-sourced half of the add-bot wizard's "cost-to-first-value"
        panel: the operator sees, at creation, the same cap figures the
        enforcers will apply once the bot exists. Every value is resolved from
        *this* config object (compiled product defaults + any pod-level
        overrides), so the panel can never drift from what's actually enforced —
        there are no hardcoded dollar figures on the UI side.

        A bot that doesn't exist yet carries no per-bot budget overrides, so the
        resolution collapses to: the in-window daily hard cap is the graduated
        new-bot default (``NEW_BOT_DAILY_HARD_USD`` — exactly what
        :meth:`new_bot_graduated_hard_cap_usd` returns for an age-0 bot), and
        the rest are pod defaults. Returned keys:

          * ``provisioning_ceiling_usd`` — one-time creation ceiling (B / the
            ``$12`` product default), the bound :mod:`provisioning_budget`
            enforces; per :meth:`provisioning_ceiling_usd`.
          * ``daily_hard_cap_usd`` — daily hard stop while the bot is inside its
            activation window (the graduated new-bot default).
          * ``daily_hard_cap_after_window_usd`` — daily hard stop once the bot
            graduates (the pod-default ``per_bot_daily_hard_usd``).
          * ``window_days`` — the graduation window (``NEW_BOT_GRADUATION_DAYS``).

        The daily *alert* figure (``daily_alert_usd``) is intentionally NOT here:
        the daily-spend alert fires on a network.json threshold, not BE config
        (see ``spend_alert.daily_spend_alert_threshold_usd``). The wizard route
        layers it on so each figure comes from the resolver that actually
        enforces it.
        """
        return {
            "provisioning_ceiling_usd": self.provisioning_ceiling_usd(None),
            "daily_hard_cap_usd": float(NEW_BOT_DAILY_HARD_USD),
            "daily_hard_cap_after_window_usd": float(
                self.resolve(None, "budget", "per_bot_daily_hard_usd")
            ),
            "window_days": float(NEW_BOT_GRADUATION_DAYS),
        }

    def monthly_cap_usd(self) -> float:
        """Pod-wide monthly spend cap."""
        return float(self.resolve(None, "budget", "monthly_cap_usd"))

    def per_bot_monthly_cap_usd(self, bot_id: str) -> float | None:
        """Operator-facing per-bot monthly cap. ``None`` if not set."""
        return self._per_bot_monthly_cap_usd_or_none(bot_id)

    def set_per_bot_monthly_cap_usd(self, bot_id: str, value: float | None) -> None:
        """Set or clear the per-bot monthly cap. Caller persists with ``save``."""
        bot = self.bots.setdefault(bot_id, {})
        budget = bot.setdefault("budget", {})
        if value is None:
            budget.pop("per_bot_monthly_cap_usd", None)
        else:
            budget["per_bot_monthly_cap_usd"] = float(value)

    def set_per_bot_daily_warn_usd(self, bot_id: str, value: float | None) -> None:
        """Set or clear the per-bot daily warn cap. Caller persists with ``save``.

        Pass ``None`` to remove the per-bot override and fall back to the pod
        default. Setting this clears any derived monthly-cap path for the warn
        threshold (the explicit daily value takes precedence per the resolution
        rule in ``budget_warn_cap_usd``).
        """
        bot = self.bots.setdefault(bot_id, {})
        budget = bot.setdefault("budget", {})
        if value is None:
            budget.pop("per_bot_daily_warn_usd", None)
        else:
            budget["per_bot_daily_warn_usd"] = float(value)

    def set_per_bot_daily_hard_usd(self, bot_id: str, value: float | None) -> None:
        """Set or clear the per-bot daily hard cap. Caller persists with ``save``."""
        bot = self.bots.setdefault(bot_id, {})
        budget = bot.setdefault("budget", {})
        if value is None:
            budget.pop("per_bot_daily_hard_usd", None)
        else:
            budget["per_bot_daily_hard_usd"] = float(value)

    def per_bot_session_cost_cap_usd(self, bot_id: str) -> float | None:
        """Operator-facing per-bot per-session cost ceiling. ``None`` if not set.

        Opt-in like ``per_bot_monthly_cap_usd``: a missing value is the
        meaningful "no cap" sentinel, distinct from a stored 0. Materialized
        into ``openclaw.json::agents.defaults.sessionBudgetCapUsd`` at deploy.
        """
        if bot_id is None:
            return None
        bot_config = self.bots.get(bot_id, {})
        value = _walk(bot_config, ("budget", "per_bot_session_cost_cap_usd"))
        if value is _SENTINEL or value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def set_per_bot_session_cost_cap_usd(
        self, bot_id: str, value: float | None
    ) -> None:
        """Set or clear the per-bot per-session cost ceiling. Caller persists with ``save``."""
        bot = self.bots.setdefault(bot_id, {})
        budget = bot.setdefault("budget", {})
        if value is None:
            budget.pop("per_bot_session_cost_cap_usd", None)
        else:
            budget["per_bot_session_cost_cap_usd"] = float(value)

    def per_bot_cache_retention(self, bot_id: str) -> str | None:
        """Operator-facing per-bot Anthropic prompt-cache TTL. ``None`` if not set.

        Returns ``"short"`` or ``"long"`` when explicitly set, else ``None``
        (inherit OC's default of "short" — the materializer skips the
        per-model fan-out when this is None).
        """
        if bot_id is None:
            return None
        bot_config = self.bots.get(bot_id, {})
        value = _walk(bot_config, ("budget", "per_bot_cache_retention"))
        if value is _SENTINEL or value is None:
            return None
        if value not in ("short", "long"):
            return None
        return value

    def set_per_bot_cache_retention(self, bot_id: str, value: str | None) -> None:
        """Set or clear the per-bot Anthropic prompt-cache TTL. Caller persists with ``save``.

        Accepts ``"short"``, ``"long"``, or ``None`` (clear override).
        """
        if value is not None and value not in ("short", "long"):
            raise ValueError(
                f"per_bot_cache_retention must be 'short', 'long', or None; got {value!r}"
            )
        bot = self.bots.setdefault(bot_id, {})
        budget = bot.setdefault("budget", {})
        if value is None:
            budget.pop("per_bot_cache_retention", None)
        else:
            budget["per_bot_cache_retention"] = value

    # ── Phase 5 of cost-cap normalization — new remediation ladder fields ─
    # Generic accessor + setter pair for the per-bot opt-in float fields
    # added in Phase 5. Spec: internal/spec-cost-caps-2026-06-05.md. Each field
    # is null by default (no enforcement at that tier); a positive float
    # enables enforcement; zero / negative / non-numeric reads as null.

    def _resolve_per_bot_opt_in_float(
        self, bot_id: str | None, field: str,
    ) -> float | None:
        """Walk ``bots.<bot>.budget.<field>``; ``None`` if absent.

        Mirrors ``_per_bot_monthly_cap_usd_or_none`` — these per-bot opt-in
        thresholds don't inherit a pod_defaults value unless the operator
        explicitly sets one at that scope (the resolver is the bot-setup
        caller's job).
        """
        if bot_id is None:
            return None
        bot_config = self.bots.get(bot_id, {})
        return _positive_float_or_none(_walk(bot_config, ("budget", field)))

    def _set_per_bot_opt_in_float(
        self, bot_id: str, field: str, value: float | None,
    ) -> None:
        """Write ``bots.<bot>.budget.<field>``; ``None`` removes the entry."""
        bot = self.bots.setdefault(bot_id, {})
        budget = bot.setdefault("budget", {})
        if value is None:
            budget.pop(field, None)
        else:
            budget[field] = float(value)

    def per_bot_tier_downgrade_usd(self, bot_id: str) -> float | None:
        """Per-bot tier-downgrade threshold. When crossed, the bot's
        primary model is auto-switched to a tier-3 model for the rest
        of the day (24h auto-revert)."""
        return self._resolve_per_bot_opt_in_float(bot_id, "tier_downgrade_usd")

    def set_per_bot_tier_downgrade_usd(
        self, bot_id: str, value: float | None,
    ) -> None:
        self._set_per_bot_opt_in_float(bot_id, "tier_downgrade_usd", value)

    def per_bot_l1_breaker_usd(self, bot_id: str) -> float | None:
        """Per-bot L1 breaker threshold. Spec-renamed view of the existing
        ``per_bot_daily_hard_usd``; storage is unchanged — Phase 8 migrates
        the field name. When crossed, heartbeat + background sessions
        pause; user chat keeps working. 24h auto-reset."""
        return self._resolve_per_bot_opt_in_float(bot_id, "per_bot_daily_hard_usd")

    def set_per_bot_l1_breaker_usd(
        self, bot_id: str, value: float | None,
    ) -> None:
        self.set_per_bot_daily_hard_usd(bot_id, value)

    def per_bot_l2_breaker_usd(self, bot_id: str) -> float | None:
        """Per-bot L2 breaker threshold. When crossed, the bot's gateway
        is stopped entirely (``launchctl bootout``) — no chat, no
        background. Manual operator reset required; no auto-revert."""
        return self._resolve_per_bot_opt_in_float(bot_id, "l2_breaker_usd")

    def set_per_bot_l2_breaker_usd(
        self, bot_id: str, value: float | None,
    ) -> None:
        self._set_per_bot_opt_in_float(bot_id, "l2_breaker_usd", value)

    def per_bot_weekly_warn_usd(self, bot_id: str) -> float | None:
        """Per-bot rolling 7-day spend warn threshold. Fires a Telegram
        alert on cross, deduped once per ISO week."""
        return self._resolve_per_bot_opt_in_float(bot_id, "weekly_warn_usd")

    def set_per_bot_weekly_warn_usd(
        self, bot_id: str, value: float | None,
    ) -> None:
        self._set_per_bot_opt_in_float(bot_id, "weekly_warn_usd", value)

    def pod_weekly_warn_usd(self) -> float | None:
        """Pod-total weekly spend warn threshold. Lives only at
        pod_defaults level (no per-bot override). ``None`` if unset.

        Phase 8 will migrate
        ``network.json::thresholds.weeklySpendAlertUsd`` into this slot.
        Until then, both fields coexist; spend_alert reads the pod
        default first and falls back to the legacy network.json field.
        """
        value = _walk(self.pod_defaults, ("budget", "pod_weekly_warn_usd"))
        if value is _SENTINEL or value is None:
            return None
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        return f if f > 0 else None

    def set_pod_weekly_warn_usd(self, value: float | None) -> None:
        """Set or clear the pod-total weekly warn threshold."""
        budget = self.pod_defaults.setdefault("budget", {})
        if value is None:
            budget.pop("pod_weekly_warn_usd", None)
        else:
            budget["pod_weekly_warn_usd"] = float(value)

    # ── The single ladder reader ─────────────────────────────────────────

    def resolve_budget_ladder(
        self, bot_id: str | None, *, now: datetime | None = None,
    ) -> dict[str, float | None]:
        """Effective value of every ladder rung for ``bot_id``.

        Returns one key per entry in :data:`BUDGET_LADDER_FIELDS`
        (``daily_warn``, ``weekly_warn``, ``tier_downgrade``, ``l1_breaker``,
        ``l2_breaker``, ``per_session``); each value is a positive float or
        ``None`` meaning "no enforcement at that rung".

        Resolution precedence, highest first:

          1. **derived from the per-bot monthly budget** — when
             ``bots.<bot>.budget.per_bot_monthly_cap_usd`` is set, ``daily_warn``
             and ``l1_breaker`` are derived from it (1.5× / 2.5× the daily
             average). Mirrors :meth:`budget_warn_cap_usd` /
             :meth:`budget_hard_cap_usd`, including the fact that a monthly
             budget outranks an explicit per-bot daily value.
          2. **explicit per-bot override** — ``bots.<bot>.budget.<field>``.
          3. **graduated new-bot default** (``l1_breaker`` only) — see
             :meth:`new_bot_graduated_hard_cap_usd`.
          4. **pod default** — ``pod_defaults.budget.<field>``.

        Step 4 is the fix for the silent-no-cap defect: before it existed, a bot
        with no explicit override resolved every rung to ``None`` and the
        enforcer skipped it entirely, while ``spend_caps.get_caps_config`` read
        the same pod defaults and told the operator a cap was in force.

        Zero / negative / non-numeric at any layer reads as "unset" and falls
        through — the same convention the per-rung accessors use. Pass
        ``bot_id=None`` for the pod-level view (steps 1–3 are skipped).

        Note on ``0``: :meth:`budget_hard_cap_usd` is presence-based and returns
        ``0.0`` for an explicit stored zero, where this returns ``None``. Both
        mean "uncapped" — callers comparing the two must treat ``0.0`` and
        ``None`` as equivalent.
        """
        derived: dict[str, float] = {}
        monthly = self._per_bot_monthly_cap_usd_or_none(bot_id)
        if monthly is not None:
            daily_avg = monthly / _DAYS_PER_MONTH
            derived["daily_warn"] = daily_avg * _WARN_MULTIPLIER
            derived["l1_breaker"] = daily_avg * _HARD_MULTIPLIER

        ladder: dict[str, float | None] = {}
        for rung, config_field in BUDGET_LADDER_FIELDS.items():
            value = derived.get(rung)
            if value is None:
                value = self._resolve_per_bot_opt_in_float(bot_id, config_field)
            if value is None and rung == "l1_breaker":
                value = self.new_bot_graduated_hard_cap_usd(bot_id, now=now)
            if value is None:
                value = _positive_float_or_none(
                    _walk(self.pod_defaults, ("budget", config_field))
                )
            ladder[rung] = value
        return ladder

    # ── Validation ───────────────────────────────────────────────────────
    def validate_remediation_ladder(self, bot_id: str) -> list[str]:
        """Return a list of validation errors for the per-bot remediation
        ladder; empty list means the ladder is well-ordered.

        Invariant: ``l2_breaker > l1_breaker > tier_downgrade > daily_warn``
        for every threshold that is explicitly set. Partial sets (any rung
        unset) are valid — a missing rung just means "no enforcement at
        that tier".
        """
        errors: list[str] = []
        warn = self.bots.get(bot_id, {}).get("budget", {}).get(
            "per_bot_daily_warn_usd"
        )
        tier = self.per_bot_tier_downgrade_usd(bot_id)
        l1 = self.per_bot_l1_breaker_usd(bot_id)
        l2 = self.per_bot_l2_breaker_usd(bot_id)

        try:
            warn_f = float(warn) if warn is not None else None
        except (TypeError, ValueError):
            warn_f = None

        def _check(low_name: str, low: float | None,
                   high_name: str, high: float | None) -> None:
            if low is not None and high is not None and not (high > low):
                errors.append(
                    f"{high_name} (${high:.2f}) must be strictly greater "
                    f"than {low_name} (${low:.2f})"
                )

        _check("daily_warn", warn_f, "tier_downgrade", tier)
        _check("tier_downgrade", tier, "l1_breaker", l1)
        _check("l1_breaker", l1, "l2_breaker", l2)
        return errors

    def _per_bot_monthly_cap_usd_or_none(self, bot_id: str | None) -> float | None:
        """Resolve per-bot monthly cap, returning None when unset.

        ``resolve()`` would normally fall back to the compiled default of
        ``None`` for this key — but ``None`` from a ``float | None`` field
        IS the meaningful "unset" sentinel here, so we don't coerce it.
        """
        if bot_id is None:
            return None
        # Direct walk into the bot override; we don't want pod-default
        # fallback for this key (if the pod doesn't set it, a bot doesn't
        # inherit a monthly cap by default).
        bot_config = self.bots.get(bot_id, {})
        value = _walk(bot_config, ("budget", "per_bot_monthly_cap_usd"))
        if value is _SENTINEL or value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    # ── Serialization ──────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "pod_defaults": _copy_nested(self.pod_defaults),
            "bots": _copy_nested(self.bots),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BetterEngineConfig":
        return cls(
            schema_version=int(data.get("schema_version", CONFIG_SCHEMA_VERSION)),
            pod_defaults=_copy_nested(data.get("pod_defaults") or {}),
            bots=_copy_nested(data.get("bots") or {}),
        )

    @classmethod
    def default(cls) -> "BetterEngineConfig":
        """Construct a config containing compiled defaults only."""
        return cls(
            schema_version=CONFIG_SCHEMA_VERSION,
            pod_defaults=_copy_nested(_COMPILED_DEFAULTS),
            bots={},
        )


# ─────────────────────────────────────────────────────────────────────────────
# IO
# ─────────────────────────────────────────────────────────────────────────────


def load(shared_dir: Path | str = Path("/Users/Shared/evolve")) -> BetterEngineConfig:
    """Load the config file from the shared dir. Falls back to defaults if absent."""
    path = Path(shared_dir) / DEFAULT_CONFIG_FILENAME
    if not path.exists():
        return BetterEngineConfig.default()
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        # Corrupt / unreadable — fall back to defaults. Callers that care
        # about distinguishing "missing" from "corrupt" can stat first.
        return BetterEngineConfig.default()
    return BetterEngineConfig.from_dict(raw)


def save(
    config: BetterEngineConfig,
    shared_dir: Path | str = Path("/Users/Shared/evolve"),
) -> Path:
    """Atomically write the config file to the shared dir.

    Uses temp-file + rename for atomicity. Creates the shared dir if it
    doesn't exist.

    The file mode is pinned to :data:`DEFAULT_CONFIG_MODE` (0644) on every
    write. ``os.replace`` carries the *temp* file's mode onto the destination,
    and ``mkstemp`` mints 0600 — so before this was pinned, one programmatic
    save silently tightened a 0644 config to 0600 and locked out every reader
    that isn't the ``evolve`` user. That breaks
    ``evolve_admin/evo/tools/pod_state_cost_caps.py``, which reads this file
    from the evo gateway subprocess running as the separate ``evo`` macOS user.
    Pinning rather than preserving is deliberate: it also self-heals a pod
    whose config was already tightened by an earlier save. This file holds
    budgets and feature toggles, never tokens — the 0600 secret-config
    contract in CLAUDE.md does not apply to it.
    """
    import os
    import tempfile

    base = Path(shared_dir)
    base.mkdir(parents=True, exist_ok=True)
    path = base / DEFAULT_CONFIG_FILENAME

    payload = json.dumps(config.to_dict(), indent=2, sort_keys=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(base), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.chmod(tmp, DEFAULT_CONFIG_MODE)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

_SENTINEL = object()


def _walk(mapping: dict, keys: tuple[str, ...]) -> Any:
    """Walk a nested dict; return _SENTINEL if any key is missing."""
    cur: Any = mapping
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return _SENTINEL
        cur = cur[k]
    return cur


def _positive_float_or_none(value: Any) -> float | None:
    """Coerce a stored budget threshold to a positive float, else ``None``.

    ``_SENTINEL`` (key absent), ``None``, non-numeric, zero and negative all
    read as "unset / no enforcement at this rung" — the convention every
    opt-in budget field shares.
    """
    if value is _SENTINEL or value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _copy_nested(value: Any) -> Any:
    """Shallow-deep copy: dicts get recursed, everything else is returned as-is."""
    if isinstance(value, dict):
        return {k: _copy_nested(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy_nested(v) for v in value]
    return value
