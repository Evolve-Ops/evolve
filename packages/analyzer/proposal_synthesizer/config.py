"""proposal_synthesizer.config — Gate thresholds and per-variant overrides.

Spec: docs/spec-proposal-synthesizer-2026-05-10.md §4.

The gate's behavior is driven by a small set of knobs:

  - **Repetition floor** (N occurrences in window W days) — §4.1
  - **Magnitude floor** per ``Magnitude.unit`` — §4.2
  - **Aggregation rule** (substrate threshold = ≥3 bots) — §4.3
  - **Concreteness exemption** per variant — §4.4

Defaults are the spec's "Phase 1 defaults." Per-variant overrides
live in this module so they're versioned in code alongside the rules
that consume them. Future revisions may move them to a YAML next to
each generator's charter; for now, code is fine.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ─────────────────────────────────────────────────────────────────────────────
# Defaults
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GateDefaults:
    """Per-rule defaults applied when no variant override exists."""

    # Repetition gate (§4.1)
    repetition_min_occurrences: int = 3
    repetition_window_days: int = 7

    # Aggregation gate (§4.3) — minimum distinct bots before substrate
    # aggregation kicks in.
    substrate_min_bots: int = 3


DEFAULTS = GateDefaults()


# Magnitude floors keyed by Magnitude.unit. A candidate's magnitude
# must be >= the floor for its unit to pass §4.2. Units not in this
# table fall through to the catch-all floor below (which is 0.0 —
# unknown units don't gate by default; the gate logs them so we can
# add explicit floors later).
MAGNITUDE_FLOORS: dict[str, float] = {
    "usd/week": 1.00,
    "usd/session": 0.50,
    "pct/share": 0.20,
    "kb": 25.0,
    "sessions/week": 5.0,
    # Ratios are dimensionless multipliers of an expected/mean
    # baseline. 1.5x is the smallest "obviously above average" ratio
    # worth surfacing — clusters running at the mean shouldn't fire,
    # and crons firing 1.1x their declared cadence are noise.
    "ratio_over_mean": 1.5,
    "ratio_over_cap": 1.0,  # below cap → not a breach, don't fire
    "ratio_over_declared": 1.5,
    # Generic "count of occurrences" — the repetition gate is the real
    # substantiveness check for these; we keep the magnitude floor at
    # 1.0 so single-instance candidates can flow when they have prior
    # history at the same fingerprint.
    "count": 1.0,
    # Generic severity scale (1=low, 2=med, 3=high, 4=critical). Floor
    # 2.0 — medium-severity is the minimum that should reach the
    # operator without further evidence.
    "severity_level": 2.0,
}
MAGNITUDE_UNKNOWN_UNIT_FLOOR: float = 0.0


@dataclass(frozen=True)
class VariantConfig:
    """Per-variant override applied to a specific (generator_id, variant).

    Any field left as ``None`` falls through to ``DEFAULTS``.
    """

    # Repetition overrides
    repetition_min_occurrences: int | None = None
    repetition_window_days: int | None = None

    # Acute exemption — when ``urgency == acute_urgency`` AND
    # ``magnitude.value >= acute_magnitude_threshold``, skip the
    # repetition gate. ``None`` for both fields means no exemption.
    acute_urgency: str | None = None
    acute_magnitude_threshold: float | None = None

    # Magnitude floor override (in the variant's magnitude unit)
    magnitude_floor: float | None = None

    # Concreteness exemption — when True, an Investigation-only
    # candidate is allowed through even without naming a tunable.
    concreteness_exempt: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# Per-variant overrides
# ─────────────────────────────────────────────────────────────────────────────
#
# Keyed by (generator_id, variant). Variants not in this table use
# DEFAULTS for every rule.

VARIANT_OVERRIDES: dict[tuple[str, str], VariantConfig] = {
    # daily_spend_high: a single-day spend cap breach is *acute* when
    # severity is alert (3x cap or worse). It should not have to wait
    # for three days of breaches to surface.
    ("efficiency_hawk", "daily_spend_high"): VariantConfig(
        acute_urgency="operational_urgent",
        acute_magnitude_threshold=3.0,  # ratio over cap; cost_watchdog
        # sets severity=alert at 3x.
        concreteness_exempt=True,  # legitimately no named tunable
        # until investigation completes.
    ),
    # cron_overactive: a cron firing 10x its declared cadence in a
    # single window is acute. The "ratio" provenance signal is the
    # magnitude here.
    ("efficiency_hawk", "cron_overactive"): VariantConfig(
        acute_urgency="operational_urgent",
        acute_magnitude_threshold=5.0,
    ),
    # tier_misrouting: when >=50% of maintenance spend is on
    # high-tier models, the finding is unambiguous and the
    # TierAdjustment action is mechanically applyable — no need to
    # wait 3 cycles. Empirically, the live pod's first cycle landed
    # security_bot at 99.7% high-tier share; the spec's intent is to
    # surface that immediately, not gate it on recurrence.
    ("efficiency_hawk", "tier_misrouting"): VariantConfig(
        acute_urgency="hygiene",
        acute_magnitude_threshold=0.50,  # pct/share
    ),
    # background_dominance: same argument as tier_misrouting. A bot
    # whose classified spend is >=70% non-user trigger_kinds is a
    # well-defined finding; the operator wants to see it on the
    # first cycle. Live pod's first cycle landed security_bot at 96.4%,
    # team_bot_c at 77.5%.
    ("efficiency_hawk", "background_dominance"): VariantConfig(
        acute_urgency="hygiene",
        acute_magnitude_threshold=0.70,  # pct/share
    ),
    # heartbeat_no_model_override: the magnitude is sessions/week
    # (derived from heartbeat cadence). >=168 sessions/week means
    # the bot heartbeats hourly or more often on the primary model —
    # the universal "set Haiku override" case. The action is concrete
    # and the operator regularly approves these; first-cycle surfacing
    # is right.
    ("efficiency_hawk", "heartbeat_no_model_override"): VariantConfig(
        acute_urgency="hygiene",
        acute_magnitude_threshold=168.0,  # sessions/week (1h cadence)
    ),
    # session_token_outlier: single-instance outliers are exactly the
    # noise case we want to suppress. Keep defaults strict.
    # context_bloat: slow-moving and rarely actionable on first
    # occurrence. Defaults are fine.
    # automation_dominance, cron_wakes_agent: bot-pattern candidates
    # that benefit from recurrence — keep defaults so a one-off
    # heartbeat spike or a misconfigured cron doesn't fire until the
    # pattern repeats.
}


def get_variant_config(
    generator_id: str, variant: str
) -> VariantConfig:
    """Return the override for this (generator, variant), or the
    empty VariantConfig if none is registered."""
    return VARIANT_OVERRIDES.get((generator_id, variant), VariantConfig())


def magnitude_floor_for(unit: str, override: float | None = None) -> float:
    """Return the magnitude floor for a unit, with optional per-variant
    override taking precedence over the unit-default table."""
    if override is not None:
        return override
    return MAGNITUDE_FLOORS.get(unit, MAGNITUDE_UNKNOWN_UNIT_FLOOR)
