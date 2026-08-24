#!/usr/bin/env python3
"""
models.py — Model tier registry and resolver.

The single source of truth for which model to use for what.
All Evolve scripts import this module instead of hardcoding model strings.

Tier definitions:
  tier0  Cross-model judge. MUST be a different provider than tier2.
         Used for proposal evaluation to avoid self-preference bias.
         Example: openai/gpt-4o when tier2 is Anthropic.

  tier1  Power tier. Top-of-line models. Most expensive.
         Used only on explicit user request or for tasks that require
         maximum capability. Should be rare — tracked by cost.py.
         Example: anthropic/claude-opus-4-6

  tier2  Workhorse tier. Default for user-facing conversations.
         The model a bot uses for most interactions.
         Example: anthropic/claude-sonnet-4-6

  tier3  Grunt tier. Cheap and fast. Used for background tasks:
         internal judgments, test running, summarization, analysis,
         anything the user doesn't see directly.
         Example: anthropic/claude-haiku-4-5

Usage:
  from models import resolve_tier, get_tier_config

  # Get the first available model for a tier
  model = resolve_tier("tier3", config)
  # → "anthropic/claude-haiku-4-5"

  # Get all models (primary + fallbacks) for a tier
  models = get_tier_models("tier3", config)
  # → ["anthropic/claude-haiku-4-5", "openai/gpt-4o-mini"]

  # Check if a model string is a tier reference
  if is_tier_ref("tier2"):
      model = resolve_tier("tier2", config)

  # Enforce usage policy (logs warning if tier1 used in background task)
  check_tier_policy("tier1", context="background_analysis")

network.json model block:
  "models": {
    "tiers": {
      "tier0": {
        "name": "Judge",
        "models": ["openai/gpt-4o"],
        "policy": "Cross-model evaluation only. Must differ from tier2 provider.",
        "costClass": "medium"
      },
      "tier1": {
        "name": "Power",
        "models": ["anthropic/claude-opus-4-6"],
        "fallbacks": [],
        "policy": "Explicit user request only. Never background tasks.",
        "maxPerDayPerBot": 10,
        "costClass": "high"
      },
      "tier2": {
        "name": "Workhorse",
        "models": ["anthropic/claude-sonnet-4-6"],
        "fallbacks": ["openai/gpt-4o"],
        "policy": "Default for user-facing conversations.",
        "costClass": "medium"
      },
      "tier3": {
        "name": "Grunt",
        "models": ["anthropic/claude-haiku-4-5"],
        "fallbacks": ["openai/gpt-4o-mini", "google/gemini-2.0-flash"],
        "policy": "Background tasks only. Fast and cheap.",
        "costClass": "low"
      }
    },
    "perBot": {
      "security_bot": { "defaultTier": "tier3" }
    }
  }
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


# ── Hardcoded defaults (used if network.json has no models block) ─────────────
# These represent the current recommended defaults.
# Update these when the model landscape changes significantly.

# ── Credential-aware default picker (2026-06-07) ─────────────────────────────
#
# The pre-2026-06-07 DEFAULT_TIERS constant hardcoded specific Anthropic
# model names. Footgun on pods without an Anthropic credential — the
# fallback chain pointed at a provider that wouldn't actually work. The
# new ``derive_default_tiers(providers)`` function builds the same dict
# shape but picks model names from whichever providers the pod actually
# has credentials for. DEFAULT_TIERS stays as a last-resort fallback when
# we genuinely don't know what's available.
#
# Provider model picks per tier, ordered by preference within each tier.
# The picker walks each tier's list in order and returns the first model
# whose provider is in the available set. If NONE match, falls through
# to DEFAULT_TIERS' Anthropic-pinned picks (because *some* answer is
# better than no answer in the resolver hot path).

# Set of LLM providers Evolve recognizes. Used to filter out non-LLM
# provider entries in auth-profiles (e.g. brave-search keys).
_KNOWN_LLM_PROVIDERS = frozenset({
    "anthropic", "openai", "google", "xai", "moonshot",
})

# Per-tier preferred model picks, by provider. Order within each tier
# is the preference order; first available provider's model wins.
# Update these when the model landscape changes (matches the spirit of
# DEFAULT_TIERS but is queryable by credential set).
_PROVIDER_PICKS: dict[str, list[tuple[str, str]]] = {
    # tier0 (Judge): cross-provider preferred — pick whichever ISN'T the
    # workhorse provider. Resolution at the call site filters to a
    # non-tier2-matching provider when possible.
    "tier0": [
        ("openai", "openai/gpt-4o"),
        ("google", "google/gemini-2.5-pro"),
        ("xai", "xai/grok-4"),
        ("moonshot", "moonshot/kimi-k3"),
        ("anthropic", "anthropic/claude-sonnet-4-6"),
    ],
    # tier1 (Power): top-tier reasoning per provider
    "tier1": [
        ("anthropic", "anthropic/claude-opus-4-6"),
        ("openai", "openai/gpt-4o"),
        ("google", "google/gemini-2.5-pro"),
        ("xai", "xai/grok-4"),
        ("moonshot", "moonshot/kimi-k3"),
    ],
    # tier2 (Workhorse): the everyday model per provider
    "tier2": [
        ("anthropic", "anthropic/claude-sonnet-4-6"),
        ("openai", "openai/gpt-4o"),
        ("google", "google/gemini-2.5-pro"),
        ("xai", "xai/grok-4"),
        ("moonshot", "moonshot/kimi-k3"),
    ],
    # tier3 (Grunt): cheap and fast per provider
    "tier3": [
        ("anthropic", "anthropic/claude-haiku-4-5"),
        ("openai", "openai/gpt-4o-mini"),
        ("google", "google/gemini-2.0-flash"),
        ("xai", "xai/grok-3-mini"),
        ("moonshot", "moonshot/kimi-k2.6"),
    ],
}

# Per-tier metadata that's provider-independent — same fields as
# DEFAULT_TIERS but model lists are filled in from the picker.
_TIER_METADATA: dict[str, dict] = {
    "tier0": {
        "name": "Judge",
        "policy": "Session-outcome evaluation. Defaults to the workhorse model; a different provider is recommended for cross-vendor checks but not required.",
        "costClass": "medium",
    },
    "tier1": {
        "name": "Power",
        "policy": "Explicit user request only. Never background tasks.",
        "maxPerDayPerBot": 10,
        "costClass": "high",
    },
    "tier2": {
        "name": "Workhorse",
        "policy": "Default for user-facing conversations.",
        "costClass": "medium",
    },
    "tier3": {
        "name": "Grunt",
        "policy": "Background tasks: analysis, testing, judging, summarization.",
        "costClass": "low",
    },
}


def derive_default_tiers(
    available_providers: set[str] | None = None,
) -> dict[str, dict]:
    """Build a credential-aware default tier dict.

    When ``available_providers`` is non-empty, picks the first model
    per tier whose provider is in the set. When the set is empty or
    None (brand-new pod, no auth-profiles readable), falls back to
    the hardcoded DEFAULT_TIERS — better to return *something* than
    crash the resolver.

    For tier0 (Judge) we additionally prefer a provider that DIFFERS
    from tier2's pick (the cross-vendor anti-Goodhart heuristic). When
    only one provider is available, tier0 falls back to that provider
    too — single-provider pods get a single-provider judge, with the
    audit layer separately surfacing the anti-Goodhart advisory.
    """
    if not available_providers:
        return {tid: dict(cfg) for tid, cfg in DEFAULT_TIERS.items()}

    def pick(tier: str, exclude_provider: str | None = None) -> str | None:
        for prov, model in _PROVIDER_PICKS.get(tier, []):
            if prov in available_providers and prov != exclude_provider:
                return model
        # If exclusion left nothing, fall back to any available
        for prov, model in _PROVIDER_PICKS.get(tier, []):
            if prov in available_providers:
                return model
        return None

    # Tier2 first — tier0 anti-Goodhart references it
    t2 = pick("tier2")
    t2_provider = t2.split("/", 1)[0] if t2 else None
    t0 = pick("tier0", exclude_provider=t2_provider)
    t1 = pick("tier1")
    t3 = pick("tier3")

    out: dict[str, dict] = {}
    for tier_id, model in [("tier0", t0), ("tier1", t1), ("tier2", t2), ("tier3", t3)]:
        # Fall back to DEFAULT_TIERS for this tier if no model derivable
        # (provider set somehow excludes every entry — shouldn't happen
        # with the set above but defensive).
        if not model:
            out[tier_id] = dict(DEFAULT_TIERS.get(tier_id, {}))
            continue
        meta = _TIER_METADATA.get(tier_id, {})
        out[tier_id] = {
            **meta,
            "models": [model],
            "fallbacks": [],
        }
    return out


def derive_seed_models(
    available_providers: set[str] | None,
) -> tuple[str, str]:
    """Return ``(primary, classifier)`` model ids for seeding a fresh
    pod's primary-bot config from the credentialed-provider set.

    primary    = the derived tier2 (Workhorse) pick — evo's
                 ``agents.defaults.model.primary`` seed.
    classifier = the derived tier3 (Grunt) pick — the evolve plugin's
                 ``classifierModel`` seed.

    Unlike ``derive_default_tiers``, this NEVER falls back to the
    Anthropic-pinned DEFAULT_TIERS: when the pod is credentialed only
    with providers that have no ``_PROVIDER_PICKS`` entry (e.g. a
    free-text provider like "mistral"), or with no providers at all,
    it returns ``("", "")`` so the caller leaves the fields unseeded
    and wizard_verify's "primary not set" check fires — loud and
    honest — instead of silently seeding a dead Anthropic model
    (docs/principle-llm-provider-agnostic.md: never presume a
    provider).
    """
    known = {p for p in (available_providers or set()) if p in _KNOWN_LLM_PROVIDERS}
    if not known:
        return "", ""
    tiers = derive_default_tiers(known)
    primary = (tiers.get("tier2", {}).get("models") or [""])[0]
    classifier = (tiers.get("tier3", {}).get("models") or [""])[0]
    return primary, classifier


def engine_default_tier_from_network(network: dict[str, Any]) -> str | None:
    """Read ``cascade.engine_default_tier`` from network.json.

    When set (e.g. "tier3"), engine-side ``resolve_tier`` calls — those
    that don't carry a bot_id — collapse to this tier regardless of
    what the caller requested. The intent is cost-control on background
    Evolve LLM work: analyzers / scanners / classifiers don't usually
    need tier1, and pinning them at tier3 (or whatever the operator
    chooses) prevents a stray tier1 call from running on a cron.

    Returns None when unset → resolve_tier honors the caller's tier as
    before. Validates to one of the known tier IDs.
    """
    if not isinstance(network, dict):
        return None
    cascade = network.get("cascade")
    if not isinstance(cascade, dict):
        return None
    t = cascade.get("engine_default_tier")
    if isinstance(t, str) and t in ("tier0", "tier1", "tier2", "tier3"):
        return t
    return None


DEFAULT_TIERS: dict[str, dict] = {
    "tier0": {
        "name": "Judge",
        "models": ["openai/gpt-4o"],
        "fallbacks": [],
        "policy": "Session-outcome evaluation. Defaults to the workhorse model; a different provider is recommended for cross-vendor checks but not required.",
        "costClass": "medium",
    },
    "tier1": {
        "name": "Power",
        "models": ["anthropic/claude-opus-4-6"],
        "fallbacks": [],
        "policy": "Explicit user request only. Never background tasks.",
        "maxPerDayPerBot": 10,
        "costClass": "high",
    },
    "tier2": {
        "name": "Workhorse",
        "models": ["anthropic/claude-sonnet-4-6"],
        "fallbacks": ["openai/gpt-4o"],
        "policy": "Default for user-facing conversations.",
        "costClass": "medium",
    },
    "tier3": {
        "name": "Grunt",
        "models": ["anthropic/claude-haiku-4-5"],
        "fallbacks": ["openai/gpt-4o-mini", "google/gemini-2.0-flash"],
        "policy": "Background tasks: analysis, testing, judging, summarization.",
        "costClass": "low",
    },
}

# Tier names for display
TIER_NAMES = {
    "tier0": "Judge (cross-model)",
    "tier1": "Power",
    "tier2": "Workhorse",
    "tier3": "Grunt",
}

# Cost classes for spend tracking. ``premium`` (added 2026-06-09 for the
# Fable-class rung, spec-model-rungs-and-roles) sorts above ``high``.
COST_CLASS_ORDER = ["low", "medium", "high", "premium"]

# ── Role ↔ legacy-tier aliasing (spec-model-rungs-and-roles-2026-06-09) ───────
#
# This module's on-disk vocabulary is still tier-keyed (tierN); the
# rungs/roles redesign for the resolver's storage is Phase 4. For Phase 1
# the resolver speaks BOTH: callers may pass a role ID (fast/standard/
# power/judge) or a legacy tier key (tier0-tier3), and resolution maps a
# role to its legacy tier before the per-tier lookup. ``max`` has no legacy
# tier — it resolves only from the new rungs/roles config shape, which this
# engine-side resolver does not yet read, so it maps to ``tier1`` (power)
# as the closest legacy fallback rather than erroring.
ROLE_TO_TIER = {
    "fast": "tier3",
    "standard": "tier2",
    "power": "tier1",
    "judge": "tier0",
    "max": "tier1",  # legacy fallback — no tierN above power
}

TIER_TO_ROLE = {
    "tier3": "fast",
    "tier2": "standard",
    "tier1": "power",
    "tier0": "judge",
}


def normalize_to_tier(tier_or_role: str) -> str:
    """Map a role ID to its legacy tier key; pass a tier key through.

    The single translation point so every resolver entry tolerates both
    vocabularies during the transition. Unknown strings pass through
    unchanged (the downstream lookup raises if truly invalid).
    """
    return ROLE_TO_TIER.get(tier_or_role, tier_or_role)


def is_role_ref(value: str) -> bool:
    """Return True if value is a role ID like 'standard', 'power', 'fast'."""
    return value in ROLE_TO_TIER


# ── Core resolver ─────────────────────────────────────────────────────────────

def resolve_tier(
    tier: str,
    config: dict[str, Any],
    bot_id: str | None = None,
    fallback_index: int = 0,
) -> str:
    """
    Resolve a tier reference to an actual model string.

    Args:
        tier: a tier key ("tier0".."tier3") OR a role ID
              ("fast"/"standard"/"power"/"judge"/"max"). Role IDs are
              normalized to their legacy tier before lookup
              (spec-model-rungs-and-roles transition tolerance).
        config: network.json config dict
        bot_id: if set, checks per-bot overrides first. When None, the
                call is an "engine-side" call (analyzer / scanner /
                classifier — not running inside a specific bot). Engine
                calls respect the ``cascade.engine_default_tier`` knob
                in network.json: when set, the requested tier is
                collapsed to that knob's value (e.g., always tier3 for
                background work) BEFORE the per-tier model lookup.
        fallback_index: 0 = primary, 1 = first fallback, etc.

    Returns:
        Model string like "anthropic/claude-sonnet-4-6"

    Raises:
        ValueError if tier is unknown and no fallback exists
    """
    # Accept a role ID or a legacy tier key (transition tolerance).
    tier = normalize_to_tier(tier)
    # Engine-default-tier override (2026-06-07): when caller has no
    # bot_id (engine-side call) AND the operator set
    # network.json::cascade.engine_default_tier, redirect this tier
    # request to that tier. Cost-control knob for background Evolve
    # work that shouldn't burn tier1 budget.
    if bot_id is None:
        forced = engine_default_tier_from_network(config)
        if forced:
            tier = forced
    tier_cfg = _get_tier_config(tier, config, bot_id)

    models = tier_cfg.get("models", [])
    fallbacks = tier_cfg.get("fallbacks", [])
    all_models = models + fallbacks

    if fallback_index < len(all_models):
        return all_models[fallback_index]

    # Last resort: return hardcoded default
    default = DEFAULT_TIERS.get(tier, {}).get("models", [])
    if default:
        return default[0]

    raise ValueError(f"No model configured for tier '{tier}'")


def get_tier_models(
    tier: str,
    config: dict[str, Any],
    bot_id: str | None = None,
) -> list[str]:
    """Get all models (primary + fallbacks) for a tier or role."""
    tier_cfg = _get_tier_config(normalize_to_tier(tier), config, bot_id)
    return tier_cfg.get("models", []) + tier_cfg.get("fallbacks", [])


def get_tier_config(
    tier: str,
    config: dict[str, Any],
    bot_id: str | None = None,
) -> dict:
    """Get the full tier config dict (accepts a tier key or role ID)."""
    return _get_tier_config(normalize_to_tier(tier), config, bot_id)


def is_tier_ref(value: str) -> bool:
    """Return True if value is a tier key ('tier1', …) or a role ID.

    Role IDs (fast/standard/power/judge/max) are accepted as tier
    references during the rungs/roles transition — they normalize to a
    legacy tier in the resolver.
    """
    return value in ("tier0", "tier1", "tier2", "tier3") or value in ROLE_TO_TIER


def get_all_tiers(config: dict[str, Any]) -> dict[str, dict]:
    """Return all tier configs.

    Reflects DEFAULT_TIERS only — the network.json::models.tiers
    pod-wide override layer was retired 2026-05-25. Per-bot
    tier_assignments still override at lookup time via
    ``_get_tier_config(bot_id=...)``; this aggregate view is the
    pod-wide default.
    """
    return {tier_id: dict(cfg) for tier_id, cfg in DEFAULT_TIERS.items()}


# ── Policy enforcement ────────────────────────────────────────────────────────

def check_tier_policy(
    tier: str,
    context: str,
    config: dict[str, Any] | None = None,
    warn_only: bool = True,
) -> None:
    """
    Log a warning if a tier is being used against its policy.
    context: brief description of the calling context (e.g. "background_analysis")

    Currently only enforces tier1 policy (should be rare).
    """
    if tier != "tier1":
        return

    background_contexts = {
        "background", "analysis", "test", "judge", "score", "classify",
        "detect", "measure", "monitor", "batch", "cron", "scheduled",
    }

    context_lower = context.lower()
    if any(kw in context_lower for kw in background_contexts):
        msg = (
            f"[evolve/models] WARNING: tier1 (Power) used in background context: "
            f"'{context}'. This is expensive — verify this is intentional."
        )
        print(msg, file=sys.stderr)


def tier_for_task(task_type: str) -> str:
    """
    Suggest appropriate tier for common task types.
    Use this when callers don't have explicit tier guidance.
    """
    task_lower = task_type.lower()

    tier1_tasks = {"complex_analysis", "novel_writing", "deep_reasoning", "architecture_review"}
    tier3_tasks = {
        "judge", "classify", "summarize", "extract", "score",
        "test_judge", "proposal_analysis", "tier_classify",
        "metric_analysis", "background", "batch",
    }

    if task_lower in tier1_tasks:
        return "tier1"
    elif task_lower in tier3_tasks:
        return "tier3"
    else:
        return "tier2"


# ── Usage tracking ────────────────────────────────────────────────────────────

def record_tier_usage(
    tier: str,
    model: str,
    context: str,
    shared_dir: Path,
    bot_id: str,
) -> None:
    """
    Record that a tier was used. Feeds into cost.py spend tracking.
    Written to shared_dir/cost/tier-usage/{bot_id}/{date}.jsonl
    """
    from datetime import datetime, timezone
    import time

    usage_dir = shared_dir / "cost" / "tier-usage" / bot_id
    usage_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    log_path = usage_dir / f"{today}.jsonl"

    record = json.dumps({
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tier": tier,
        "model": model,
        "context": context,
        "bot_id": bot_id,
    })

    with log_path.open("a") as f:
        f.write(record + "\n")


def get_tier_usage_today(
    bot_id: str,
    shared_dir: Path,
    tier: str | None = None,
) -> dict[str, int]:
    """
    Return today's usage counts per tier (or for a specific tier).
    Returns {tier_id: count}.
    """
    from datetime import datetime

    today = datetime.now().strftime("%Y-%m-%d")
    log_path = shared_dir / "cost" / "tier-usage" / bot_id / f"{today}.jsonl"

    if not log_path.exists():
        return {}

    counts: dict[str, int] = {}
    try:
        for line in log_path.read_text().splitlines():
            record = json.loads(line)
            t = record.get("tier", "unknown")
            if tier is None or t == tier:
                counts[t] = counts.get(t, 0) + 1
    except Exception:
        pass

    return counts


def check_daily_limit(
    tier: str,
    bot_id: str,
    shared_dir: Path,
    config: dict[str, Any],
) -> tuple[bool, int, int | None]:
    """
    Check whether a bot has hit its daily tier1 limit.
    Returns (within_limit, current_count, limit).
    """
    tier_cfg = _get_tier_config(tier, config, bot_id)
    limit = tier_cfg.get("maxPerDayPerBot")

    if limit is None:
        return True, 0, None  # No limit configured

    counts = get_tier_usage_today(bot_id, shared_dir, tier=tier)
    current = counts.get(tier, 0)
    return current < limit, current, limit


# ── Session-aware routing ────────────────────────────────────────────────────

def select_model_for_session(
    session_class: str,
    user_requested_power: bool = False,
    is_background: bool = False,
    config: dict[str, Any] | None = None,
    bot_id: str | None = None,
) -> tuple[str, str]:
    """
    Select the appropriate model tier and model string for a session,
    given what we know about it.

    Args:
        session_class:         "productive" | "maintenance" | "ambiguous"
        user_requested_power:  True if the user explicitly asked for Opus/best
        is_background:         True if this is an internal/Evolve background call
        config:                network.json dict (uses defaults if None)
        bot_id:                for per-bot overrides

    Returns:
        (tier, model_string) e.g. ("tier2", "anthropic/claude-sonnet-4-6")

    Routing logic:
        background              → tier3 (grunt, always cheap for internal work)
        user_requested_power    → tier1 (power, explicit user override)
        productive + ambiguous  → tier2 (workhorse, default for real work)
        maintenance             → tier3 (maintenance sessions rarely need power;
                                         if we’re debugging a config error, Haiku
                                         is sufficient and we’re spending tokens
                                         on system overhead, not user value)
    """
    cfg = config or {}

    if is_background:
        tier = "tier3"
    elif user_requested_power:
        tier = "tier1"
        check_tier_policy("tier1", "user-requested", cfg)
    elif session_class == "maintenance":
        # Maintenance sessions use tier3 by default.
        # The assumption: if the bot is debugging its own config, it doesn’t
        # need Sonnet-class reasoning; it needs to be responsive and cheap.
        # Operators can override this via network.json models.routing.maintenance_tier.
        routing = cfg.get("models", {}).get("routing", {})
        tier = routing.get("maintenance_tier", "tier3")
    else:
        # productive or ambiguous: use workhorse
        tier = "tier2"

    model = resolve_tier(tier, cfg, bot_id)
    return tier, model


def explain_model_selection(
    session_class: str,
    user_requested_power: bool = False,
    is_background: bool = False,
    config: dict[str, Any] | None = None,
) -> str:
    """
    Return a human-readable explanation of why a model was selected.
    Useful for auditing and operator visibility.
    """
    tier, model = select_model_for_session(
        session_class, user_requested_power, is_background, config
    )
    if is_background:
        reason = "background/internal task → tier3 (grunt)"
    elif user_requested_power:
        reason = "user requested power mode → tier1 (power)"
    elif session_class == "maintenance":
        routing = (config or {}).get("models", {}).get("routing", {})
        default = routing.get("maintenance_tier", "tier3")
        reason = f"maintenance session → {default} (configurable via models.routing.maintenance_tier)"
    else:
        reason = f"{session_class} session → tier2 (workhorse)"
    return f"{model} ({tier}): {reason}"


# ── Display helpers ───────────────────────────────────────────────────────────

def print_tier_table(config: dict[str, Any]) -> None:
    """Print a human-readable table of all tiers and their current models."""
    tiers = get_all_tiers(config)

    col_w = [8, 12, 45, 10]
    header = f"  {'Tier':<{col_w[0]}} {'Name':<{col_w[1]}} {'Models':<{col_w[2]}} {'Cost':<{col_w[3]}}"
    print()
    print(header)
    print("  " + "-" * sum(col_w))

    for tier_id in sorted(tiers.keys()):
        cfg = tiers[tier_id]
        models = cfg.get("models", [])
        fallbacks = cfg.get("fallbacks", [])
        primary = models[0] if models else "—"
        fb_str = f" (+{len(fallbacks)} fallback{'s' if len(fallbacks)>1 else ''})" if fallbacks else ""
        cost = cfg.get("costClass", "?")
        name = cfg.get("name", tier_id)
        print(f"  {tier_id:<{col_w[0]}} {name:<{col_w[1]}} {primary + fb_str:<{col_w[2]}} {cost:<{col_w[3]}}")

    print()


_TIER_TO_ROLE_DISPLAY: dict[str, str] = {
    "tier3": "fast",
    "tier2": "standard",
    "tier1": "power",
    "tier0": "judge",
}

# Plain-language description of each provenance layer (spec §Addendum 2.6 —
# `evolve-admin models show` displays layer provenance per role).
_LAYER_DESC: dict[str, str] = {
    "default": "Evolve default (shipped in code; override per-pod or per-bot)",
    "pod": "network.json (pod-wide override)",
    "bot": "primary bot's own definitions (evolve-tiers.json)",
}


def print_tier_detail(tier: str, config: dict[str, Any]) -> None:
    """Print full details for one tier (accepts a tier key or role ID)."""
    # Capture the original role token before normalization: ``max`` normalizes
    # to ``tier1`` (no legacy tierN above power), so the post-normalize tier is
    # ambiguous for provenance — ``max`` and ``power`` both map to tier1.
    requested_role = tier if tier in {"fast", "standard", "power", "max", "judge"} else None
    tier = normalize_to_tier(tier)
    cfg = _get_tier_config(tier, config)
    print(f"\n  {tier} — {cfg.get('name','?')} (cost: {cfg.get('costClass','?')})")
    print(f"  Policy: {cfg.get('policy','—')}")
    print(f"  Models (primary): {', '.join(cfg.get('models', ['—']))}")
    if cfg.get("fallbacks"):
        print(f"  Fallbacks:        {', '.join(cfg['fallbacks'])}")
    if cfg.get("maxPerDayPerBot"):
        print(f"  Daily limit:      {cfg['maxPerDayPerBot']} uses/bot")

    # Layer provenance — which config layer (default / pod / bot) decided this
    # role's rung. Resolves the role (max included) through the same
    # defaults ← pod ← bot merge the loaders use.
    role = requested_role or _TIER_TO_ROLE_DISPLAY.get(tier, tier)
    try:
        from primary_bot import (  # type: ignore
            primary_bot_id,
            resolve_roles_with_provenance,
        )
        prov = resolve_roles_with_provenance(config, primary_bot_id(config))
        entry = prov.get(role)
        if entry:
            layer = entry.get("layer", "default")
            desc = _LAYER_DESC.get(layer, layer)
            print(f"  Source layer:     {layer} — {desc}")
            if entry.get("rung"):
                print(f"  Rung:             {entry['rung']}")
    except Exception as exc:
        # Provenance is a display nicety on top of the already-printed tier
        # detail — never let it crash `models show`. Surface a one-line note
        # instead of swallowing silently so a broken resolver stays visible.
        print(f"  Source layer:     (unavailable — {type(exc).__name__})")
    print()


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_tier_config(
    tier: str,
    config: dict[str, Any],
    bot_id: str | None = None,
) -> dict:
    """
    Merge tier config: defaults → per-bot tier_assignments.

    When ``bot_id`` is None the primary bot (``network.json → primary``)
    is used so engine background calls (analyzer / scanner / help-bot /
    spec-extractor) inherit the primary bot's tier choices set on the
    AI Optimization page. The Evolve admin layer is conceptually part of
    evo — they share LLM tier policy. One config layer, not two.

    Note: the pre-2026-05-25 ``network.json → models.tiers`` pod-wide
    override layer was removed. It sat between defaults and
    tier_assignments and was silently shadowed any time the primary had
    per-bot assignments set (which is almost always after AI Optimization
    has been touched). Removing it eliminates the silent-shadowing
    footgun. If a future need for divergent admin-layer LLM calls
    arises, that's better expressed as a per-call override at the call
    site, not a pod-wide config knob.
    """
    base = dict(DEFAULT_TIERS.get(tier, {}))
    merged = dict(base)

    # Default to primary bot when no bot specified — engine calls inherit
    # the operator's per-bot tier assignments rather than DEFAULT_TIERS.
    if bot_id is None:
        from primary_bot import primary_bot_id  # type: ignore

        bot_id = primary_bot_id(config)

    if bot_id:
        # Per-bot tier models live in <bot_home>/.openclaw/evolve-tiers.json,
        # written by AI Optimization → Save Tiers (PUT /api/admin/config/
        # <bot>/tiers → oc_model.py config set). See bot_tier_models()
        # for the storage history — the read path was previously broken
        # (looked at network.json::models.tier_assignments which no UI
        # flow writes to); fixed 2026-05-25 in PR #1544.
        from primary_bot import bot_tier_models  # type: ignore

        bot_assigned = bot_tier_models(config, bot_id, tier)
        if bot_assigned:
            merged = {**merged, "models": list(bot_assigned)}
        elif tier == "tier0":
            # Judge defaults to the workhorse (tier2) model when the bot
            # hasn't picked one explicitly. A distinct cross-provider
            # judge is recommended but not required — forcing every bot
            # to maintain two LLM credentials just to take a turn is bad
            # UX. Falling back to DEFAULT_TIERS["tier0"] (openai/gpt-4o)
            # would error at call time on bots without OpenAI keys.
            tier2_assigned = bot_tier_models(config, bot_id, "tier2")
            if tier2_assigned:
                merged = {**merged, "models": list(tier2_assigned)}
            else:
                t2_default = DEFAULT_TIERS.get("tier2", {}).get("models", [])
                if t2_default:
                    merged = {**merged, "models": list(t2_default)}

        # Legacy: models.perBot[bot].defaultTier — currently informational only.
        per_bot = config.get("models", {}).get("perBot", {}).get(bot_id, {})
        _ = per_bot  # reserved for future use

    return merged
