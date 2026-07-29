"""cost_opt_tiles.py — per-bot tile data for the Cost Optimization page.

Surfaces three layers at a glance for each bot:

  1. **Grade** — the existing 0-100 ``compute_cost_score`` promoted to
     a tile-level letter (A/B/C/D/F) with sub-totals (Config / Behavior).
  2. **Model mix** — 7-day routing outcome, bucketed by TIER (not by
     specific model). Visual story is uniform across bots:
     ``more green (tier3) = healthier`` for member bots dominantly doing
     maintenance work. Aggregated from the daily cost-rollup files in
     ``{shared_dir}/metrics/{bot}/cost-{YYYY-MM-DD}.json``.
  3. **Chip row** — active cost-domain issues, max 3 visible:
     ``breaker_tripped`` / ``runaway_active`` (red), ``cost_spike`` /
     ``config_drift`` / ``cap_close`` / ``bloat`` / ``cache_low``
     (yellow), ``cascade_live`` (informational green — marks the
     treatment group during the cascade rollout experiment).
     (Historic ``primary_off_floor`` chip retired 2026-06-04 alongside
     primary_model_floor_advisor; see runner retirement note.)

This module is read-only; it composes results from existing producers
(``session_monitor``, ``tile_metrics``, ``cost_watchdog`` Signals,
the breakers/runaway state files, ``evolve-tiers.json``). Adding a
new chip means adding a detector here OR layering on an existing
Signal — no new data pipeline.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

# ─────────────────────────────────────────────────────────────────────────────
# Grade letter mapping
# ─────────────────────────────────────────────────────────────────────────────

# Score → grade letter. Threshold values chosen so an operator scanning
# the row gets the same signal across all bots — A means "nothing for
# you to do here"; F means "this bot needs immediate attention." The
# cliff between B and C (75) is deliberate: B-grade bots have minor
# tuning gaps; C-grade have multiple components failing.
_GRADE_THRESHOLDS: tuple[tuple[int, str], ...] = (
    (90, "A"),
    (80, "B"),
    (70, "C"),
    (60, "D"),
    (0, "F"),
)


def _grade_letter(score: int) -> str:
    for threshold, letter in _GRADE_THRESHOLDS:
        if score >= threshold:
            return letter
    return "F"


# ─────────────────────────────────────────────────────────────────────────────
# Model → tier mapping
# ─────────────────────────────────────────────────────────────────────────────

# Map a model id (case-insensitive substring match) to its operational
# tier. Tier names match ``evolve-tiers.json`` conventions so the tile
# bar shares alias semantics with the rest of the tier-routing stack.
#
# This is a coarse-grained mapping for VISUAL grouping; precise tier
# resolution per-bot lives in ``ModelRouter.getTierForModel`` and uses
# each bot's own tier config. The tile only cares about the bucket
# (tier3 = cheap, tier2 = workhorse, tier1 = premium).
# Bucket labels stay tier-keyed (tierN) — this is the wire format the
# admin-UI mix-bar JS reads, and rewriting it to role IDs is part of the
# Phase 2 UI surface, not Phase 1. The most specific patterns must come
# first: ``fable`` before ``opus`` so the Fable-class frontier model lands
# in its own ``premium`` bucket rather than missing and rendering as
# "unknown".
#
# Phase 4 review follow-up F1 (spec-model-rungs-and-roles-2026-06-09):
# ``fable`` gets its own ``premium`` bucket distinct from ``tier1``
# (opus-class). Folding Fable into tier1 hid the 2× Fable spend inside
# the opus segment of the 7-day mix tile — an operator couldn't see how
# much of "power" spend was actually frontier-model spend. The premium
# bucket is the visual mirror of ``costClass: "premium"`` on the
# fable-class rung.
_MODEL_TIER_BUCKETS: tuple[tuple[str, str], ...] = (
    # Fable-class (frontier / premium) — its own bucket so 2× Fable spend
    # is visible separately from opus-class power spend. Listed first so it
    # wins over any broader Anthropic substring.
    ("fable",                "premium"),
    # tier3 (cheap / floor)
    ("haiku",                "tier3"),
    ("gpt-4o-mini",          "tier3"),
    ("gemini-2.0-flash",     "tier3"),
    ("gemini-1.5-flash",     "tier3"),
    ("grok-3-mini",          "tier3"),
    ("grok-4-mini",          "tier3"),
    # tier2 (workhorse)
    ("sonnet",               "tier2"),
    ("gpt-4o",               "tier2"),
    ("gpt-4.1",              "tier2"),
    ("gemini-2.5-pro",       "tier2"),
    ("gemini-3.1-pro",       "tier2"),
    ("grok-4",               "tier2"),
    ("grok-3",               "tier2"),
    # tier1 (premium / power)
    ("opus",                 "tier1"),
    # tier0 (judge / cross-provider)
    ("judge",                "tier0"),
)


def _model_to_tier(model: str) -> str:
    """Bucket a model id to its tier for visual grouping. Returns
    "unknown" when the id doesn't match any family — the chip then
    shows up as gray in the mix bar so operators see a real gap
    instead of silently misattributing the spend."""
    if not model:
        return "unknown"
    needle = str(model).lower()
    for pattern, tier in _MODEL_TIER_BUCKETS:
        if pattern in needle:
            return tier
    return "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Model-mix aggregation
# ─────────────────────────────────────────────────────────────────────────────


def aggregate_model_mix(
    shared_dir: Path, bot_id: str, *, days: int = 7, today: date | None = None,
) -> dict:
    """Aggregate per-tier turn / cost share for the last ``days`` days.

    Reads ``{shared_dir}/metrics/{bot_id}/cost-{date}.json`` (one file
    per day) and sums each entry's ``event_count`` and ``cost_usd``
    bucketed by tier.

    Returns::

        {
          "total_turns": int,
          "total_cost":  float,
          "by_tier": [                # sorted by descending share
            {
              "tier":  "tier3",
              "turns": int,
              "cost":  float,
              "turn_share": float,    # 0..1
              "cost_share": float,    # 0..1
              "dominant_model": str,  # e.g. "anthropic/claude-haiku-4-5"
            },
            ...
          ],
        }

    Returns an empty-state dict (``total_turns=0``, empty ``by_tier``)
    when no rollup files exist for the window — the tile UI renders
    that as a gray "no data" bar instead of pretending zero turns
    means everything is on tier3.
    """
    today = today or date.today()
    tier_turns: dict[str, int] = {}
    tier_cost: dict[str, float] = {}
    tier_dominant_model: dict[str, tuple[str, float]] = {}

    for offset in range(days):
        d = today - timedelta(days=offset)
        rollup_path = (
            shared_dir / "metrics" / bot_id / f"cost-{d.isoformat()}.json"
        )
        if not rollup_path.exists():
            continue
        try:
            data = json.loads(rollup_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        by_model = data.get("by_model") or {}
        if not isinstance(by_model, dict):
            continue
        for model_id, bucket in by_model.items():
            if not isinstance(bucket, dict):
                continue
            tier = _model_to_tier(model_id)
            n = int(bucket.get("event_count", 0) or 0)
            c = float(bucket.get("cost_usd", 0.0) or 0.0)
            tier_turns[tier] = tier_turns.get(tier, 0) + n
            tier_cost[tier] = tier_cost.get(tier, 0.0) + c
            # Track the dominant model within each tier so the tooltip
            # can surface "haiku 78%" instead of just "tier3 78%" —
            # operators want to see WHICH haiku version when there are
            # multiple in tier3 (e.g. claude-haiku-4-5 vs gpt-4o-mini).
            prev = tier_dominant_model.get(tier)
            if prev is None or c > prev[1]:
                tier_dominant_model[tier] = (model_id, c)

    total_turns = sum(tier_turns.values())
    total_cost = sum(tier_cost.values())

    by_tier: list[dict] = []
    for tier in sorted(tier_turns.keys(), key=lambda t: -tier_turns[t]):
        turns = tier_turns[tier]
        cost = tier_cost.get(tier, 0.0)
        dom = tier_dominant_model.get(tier, (None, 0.0))[0]
        by_tier.append({
            "tier": tier,
            "turns": turns,
            "cost": round(cost, 4),
            "turn_share": (turns / total_turns) if total_turns else 0.0,
            "cost_share": (cost / total_cost) if total_cost else 0.0,
            "dominant_model": dom,
        })

    return {
        "total_turns": total_turns,
        "total_cost": round(total_cost, 4),
        "by_tier": by_tier,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Chip detectors that don't already live in tile_metrics
# ─────────────────────────────────────────────────────────────────────────────


def detect_cascade_chip(shared_dir: Path, bot_id: str, bot_user: str | None) -> dict | None:
    """Return an informational chip when cascade routing is live on this
    bot — marks the treatment group during the rollout experiment so the
    operator can scan who's flipped at a glance.

    Reads ``/Users/<bot_user>/.openclaw/evolve-tiers.json::cascade.enabled``.
    Returns None when:
      - the file doesn't exist
      - cascade key absent
      - cascade.enabled is false
    """
    if not bot_user:
        return None
    tiers_path = Path(f"/Users/{bot_user}/.openclaw/evolve-tiers.json")
    try:
        data = json.loads(tiers_path.read_text())
    except (OSError, json.JSONDecodeError, PermissionError):
        return None
    cascade = data.get("cascade") or {}
    if not isinstance(cascade, dict) or not cascade.get("enabled"):
        return None
    return {
        "id": "cascade_live",
        "severity": "info",
        "label": "cascade",
        "detail": "treatment group — cascade routing live",
    }


def _breaker_expired(expires_at: Any) -> bool:
    """True iff ``expires_at`` is a parseable ISO timestamp in the past.

    Mirrors ``breakers.store.is_expired`` without importing the module —
    this file runs in slim analyzer subprocess contexts where pulling
    in breakers.store adds startup cost for one helper. Fail-open on
    parse errors (don't accidentally clear a chip on a malformed file).
    """
    if not expires_at:
        return False
    raw = str(expires_at)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        exp = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= exp


def detect_breaker_tripped_chip(shared_dir: Path, bot_id: str) -> dict | None:
    """Return a red chip when this bot's L1 cost breaker is currently
    tripped. Reads ``{shared_dir}/breakers/<bot>/cost.json`` and
    surfaces when the breaker is in the active-trip state.

    Returns None when:
      - no breaker file for this bot
      - breaker exists but is in cleared / armed state
      - file unreadable

    Crosses with the existing breakers page; the chip is the at-a-glance
    indicator so an operator scanning the row sees the bot in red and
    knows where to click.
    """
    breaker_path = shared_dir / "breakers" / bot_id / "cost.json"
    try:
        data = json.loads(breaker_path.read_text())
    except (OSError, json.JSONDecodeError, PermissionError):
        return None
    # The breaker file has a `tripped_at` field set when active and an
    # `expires_at` field for auto-cleared trips. `reset()` deletes the
    # file outright, but expiry doesn't — the Phase-3 reaper does, and
    # until it runs the file lingers (see breakers/store.py §Semantics).
    # Treat past-expiry as cleared so the chip drops as soon as the TTL
    # ends, without waiting for the reaper.
    tripped_at = data.get("tripped_at")
    if not tripped_at:
        return None
    if _breaker_expired(data.get("expires_at")):
        return None
    return {
        "id": "breaker_tripped",
        "severity": "critical",
        "label": "breaker tripped",
        "detail": "L1 cost breaker active — auto turns suspended",
        "nav": "breakers",
    }


# detect_primary_off_floor_chip retired 2026-06-04 alongside
# primary_model_floor_advisor — the generator that fed this chip. The
# chip's "lower primary to the floor tier" framing collapsed the
# distinction between background work (already routed via the trigger
# anchor) and human-chat sessions (intentionally on Sonnet per PR #1774).
# Operator-driven default-tier tuning now happens through Phase A's
# userTierOverride.defaultTier picker on the AI Optimization page.
# See docs/decision-retire-primary-model-floor-advisor-2026-06-04.md.
    return None


def detect_config_drift_chip(
    shared_dir: Path, bot_id: str,
) -> dict | None:
    """Return a yellow chip when the ``cost_watchdog.detect_config_drift``
    Signal is firing for this bot — load-bearing openclaw.json field
    has changed unexpectedly.

    Sanctioned read path (spec-state-store-and-deploy-resilience §1.1
    Phase B): goes through ``signals.store.iter_active`` (filtered by
    producer + bot_id server-side) rather than globbing
    ``{shared_dir}/signals/firing/`` directly. Surfaces the drifted
    dotpath in the chip detail so the operator sees which field.

    NOTE (Phase B): the pre-migration raw-JSON reader filtered on a
    top-level ``kind == "config_drift"`` field. cost_watchdog emits the
    drift discriminator in ``type`` (``config_drift``), never ``kind``
    (see cost_watchdog.py make_signature(PRODUCER, "config_drift", ...)),
    so that check NEVER matched a real Signal — the chip was silently
    dead. Routing through the typed store API surfaced the mismatch; the
    filter now reads ``type``, which is what the chip's docstring and its
    test ("fires_on_firing_signal") always intended. This is a latent-bug
    fix riding along with the reader migration, not a behavior change to a
    path that ever worked.
    """
    try:
        from signals import store as signals_store  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        candidates = list(
            signals_store.iter_active(
                shared_dir, producer="cost_watchdog", bot_id=bot_id, state="firing"
            )
        )
    except Exception:
        return None
    for sig in candidates:
        data = sig.to_dict()
        if data.get("type") != "config_drift":
            continue
        details = data.get("details") or {}
        dotpath = details.get("dotpath") or "openclaw.json field"
        return {
            "id": "config_drift",
            "severity": "warn",
            "label": "config drift",
            "detail": f"{dotpath} changed unexpectedly",
            "nav": "cost-optimization",
        }
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Chip prioritization + truncation
# ─────────────────────────────────────────────────────────────────────────────


# Chips are sorted by this severity rank before truncation — critical
# always shows; warn fills remaining slots; info is the tail.
_SEVERITY_RANK = {"critical": 0, "warn": 1, "info": 2}

# Within a severity, chip-id ranking nudges the most-actionable issues
# above informational ones.
_CHIP_ID_RANK: dict[str, int] = {
    "breaker_tripped":    0,
    "runaway_active":     1,
    "cost_spike":         2,
    # rank 3 left as a gap rather than renumbering — keeps any other
    # surface that hard-codes a rank stable; primary_off_floor retired
    # 2026-06-04 alongside its generator.
    "model_shift":        4,
    "config_drift":       5,
    "cap_close":          6,
    "bloat":              7,
    "cache_low":          8,
    "cascade_live":       99,  # always tail
}


def prioritize_chips(chips: list[dict], *, max_visible: int = 3) -> list[dict]:
    """Sort by (severity, chip-id rank) and truncate.

    Critical chips never drop. Warn chips fill remaining slots. Info
    chips (cascade_live) only appear when slots remain after warn
    consumption — except when the chip row would otherwise be empty,
    in which case we promote one info chip so the tile isn't blank.
    """
    if not chips:
        return []
    sorted_chips = sorted(
        chips,
        key=lambda c: (
            _SEVERITY_RANK.get(c.get("severity", "info"), 3),
            _CHIP_ID_RANK.get(c.get("id", ""), 50),
        ),
    )
    visible = sorted_chips[:max_visible]
    return visible


# ─────────────────────────────────────────────────────────────────────────────
# Top-level tile assembly
# ─────────────────────────────────────────────────────────────────────────────


def build_tile(
    shared_dir: Path,
    bot_id: str,
    bot_data: dict,
    *,
    network: dict | None = None,
    today: date | None = None,
    openclaw_settings: dict | None = None,
    extra_chips: Iterable[dict] | None = None,
) -> dict:
    """Assemble one tile's data block.

    ``bot_data`` is the per-bot dict from ``status.network_status`` —
    same shape that ``tile_metrics.compute_tile_data`` consumes.

    ``openclaw_settings`` is the bot's openclaw.json (or the cost-
    relevant subset). Caller passes it in rather than re-deriving so
    one read serves the tile + the per-bot detail page.

    Returns::

        {
          "bot_id": str,
          "grade": "A"|"B"|"C"|"D"|"F",
          "score": int,
          "spend": {"usd_28d": float, "delta_pct_28d": float | None},
          "model_mix": {                  # from aggregate_model_mix
              "total_turns": int,
              "total_cost":  float,
              "by_tier": [...],
          },
          "chips": [...],
        }
    """
    today = today or date.today()
    shared_dir = Path(shared_dir)

    # Cost score + grade — reuse session_monitor's existing computation
    try:
        from session_monitor import compute_cost_score
    except ImportError:
        # Fail-open shape so the tile renders even when session_monitor
        # can't be imported (test-isolated environments).
        score_data = {"score": 0, "components": []}
    else:
        try:
            score_data = compute_cost_score(
                shared_dir, bot_id, openclaw_settings, days=7,
            )
        except Exception:
            score_data = {"score": 0, "components": []}

    total_score = int(score_data.get("score", 0))

    # Spend headline (28d) + delta — reuse tile_metrics
    spend_usd_28d = 0.0
    spend_prior_28d = 0.0
    try:
        from tile_metrics import compute_tile_data
        tile = compute_tile_data(
            shared_dir=shared_dir,
            bot_id=bot_id,
            bot_data=bot_data,
            network=network,
            today=today,
        )
        spend_usd_28d = float(tile.get("cost", {}).get("usd_28d", 0.0))
        spend_prior_28d = float(tile.get("cost", {}).get("usd_prior_28d", 0.0))
        existing_chips: list[dict] = list(tile.get("health_chips") or [])
    except Exception:
        existing_chips = []

    delta_pct: float | None = None
    if spend_prior_28d > 0:
        delta_pct = (spend_usd_28d - spend_prior_28d) / spend_prior_28d * 100

    # Model mix — aggregated from cost rollups
    mix = aggregate_model_mix(shared_dir, bot_id, days=7, today=today)

    # New chip detectors that the cost-opt tile owns (existing
    # tile_metrics chips like cost_spike already came through above)
    chips: list[dict] = list(existing_chips)
    bot_user = None
    if isinstance(network, dict):
        bot_user = (
            (network.get("bots") or {}).get(bot_id, {}).get("user")
            or bot_id
        )

    for detector in (
        detect_breaker_tripped_chip,
        detect_config_drift_chip,
    ):
        try:
            chip = detector(shared_dir, bot_id)
        except Exception:
            chip = None
        if chip is not None:
            chips.append(chip)

    cascade_chip = detect_cascade_chip(shared_dir, bot_id, bot_user)
    if cascade_chip is not None:
        chips.append(cascade_chip)

    if extra_chips:
        for c in extra_chips:
            if isinstance(c, dict):
                chips.append(c)

    chips = prioritize_chips(chips, max_visible=3)

    return {
        "bot_id": bot_id,
        "grade": _grade_letter(total_score),
        "score": total_score,
        "spend": {
            "usd_28d": round(spend_usd_28d, 2),
            "usd_prior_28d": round(spend_prior_28d, 2),
            "delta_pct_28d": (
                round(delta_pct, 1) if delta_pct is not None else None
            ),
        },
        "model_mix": mix,
        "chips": chips,
    }


def build_all_tiles(
    shared_dir: Path,
    bot_ids: Iterable[str],
    network: dict,
    *,
    today: date | None = None,
    bot_data_resolver: Any | None = None,
    settings_resolver: Any | None = None,
) -> list[dict]:
    """Build tiles for a list of bots, preserving input order.

    ``bot_data_resolver(bot_id) -> dict`` returns the per-bot status
    dict the tile needs; falls back to a minimal dict when None.

    ``settings_resolver(bot_id) -> dict`` returns the bot's openclaw.json
    (or the cost-relevant subset). Falls back to {} when None.
    """
    tiles: list[dict] = []
    for bot_id in bot_ids:
        bot_data = (
            bot_data_resolver(bot_id) if bot_data_resolver else
            {"role": "member"}
        )
        settings = (
            settings_resolver(bot_id) if settings_resolver else {}
        )
        tile = build_tile(
            shared_dir=shared_dir,
            bot_id=bot_id,
            bot_data=bot_data,
            network=network,
            today=today,
            openclaw_settings=settings,
        )
        tiles.append(tile)
    return tiles
