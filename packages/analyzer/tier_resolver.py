"""tier_resolver.py — Compose OC signals + tier policy into a current recommendation.

Why this exists
===============

For two years Evolve recommended models from a hardcoded
``model_registry.RECOMMENDED`` dict that aged out the moment any
provider shipped. The freshness check would then push operators
*backward* to retired model names, which OC v2026.6.1's
``legacy-config-migrations`` would silently prune, which would trip the
catalog drift advisory, which the operator would click "Reconcile" on,
which would re-add the retired name, which OC would prune again. Loop.

The fix is to stop curating model NAMES and start curating
*tier-assignment RULES*. The rules are small and stable ("openai
workhorse = OC's default constant; openai cheap-floor = OC's
``gpt-mini`` alias"). The actual model the rule resolves to comes from
OC's own bundled signals — :mod:`oc_introspection` extracts those.

When OC updates ("anthropic shipped opus-4-8 today"), our recommendation
updates automatically because OC's alias map already moved. We only
touch this file when a *provider* invents a new family naming convention
(rare — anthropic's Opus/Sonnet/Haiku triad is years stable).

Resolution flow
===============

For each (provider, tier) the freshness check needs an answer for, we:

  1. Look up the policy rule (a 2-3 line spec like ``alias=opus``).
  2. Apply the rule against OC signals (alias map, default constants).
  3. Verify the resolved model isn't in the retirement set — if it
     somehow is, hop to the replacement.
  4. If nothing resolves (signals empty, unknown provider, unknown
     tier), return ``None``. Callers fall back to the static
     :data:`model_registry.RECOMMENDED` dict.

The static fallback isn't there because we trust it; it's there because
"freshness check throws on missing OC dist" is worse than "freshness
check shows last-known recommendations."
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from oc_introspection import OcSignals, load_oc_signals


# ── Tier policy ────────────────────────────────────────────────────────────


Strategy = Literal["default_constant", "alias"]


@dataclass(frozen=True)
class TierRule:
    """One tier-assignment rule for one (provider, tier) cell.

    ``strategy``:
      - ``default_constant``: use OC's ``<PROVIDER>_DEFAULT_MODEL``
        bundled constant. Authoritative "if the operator didn't pick,
        here's what OC would route to."
      - ``alias``: use ``OcSignals.aliases[alias_key]``. The alias map
        encodes OC's tier intent directly (``opus`` = power family;
        ``gpt-mini`` = small fast workhorse for openai). OC keeps this
        map current per release.
    """

    strategy: Strategy
    alias_key: str | None = None  # required when strategy == "alias"


# Per-provider, per-tier rules. Coverage is intentionally NOT total:
# entries we don't list fall through to the static
# ``model_registry.RECOMMENDED`` dict — that's the graceful-degrade
# escape hatch when OC doesn't expose a signal for some family yet
# (notably xAI, which has no alias entries in OC v2026.6.1).
TIER_POLICY: dict[str, dict[str, TierRule]] = {
    # OpenAI: OC publishes both a ``OPENAI_DEFAULT_MODEL`` constant
    # (currently gpt-5.5 — the routing default) AND ``gpt``/``gpt-mini``
    # /``gpt-nano`` aliases (currently gpt-5.4 family — the alias
    # surface). They differ by one minor version because OC's routing
    # default updates first; the alias map updates one release behind.
    # We use the default-constant for workhorse/power tiers (it's the
    # more aggressive "current" pointer) and the ``gpt-mini`` alias for
    # the floor (no default-mini constant exists).
    "openai": {
        "tier0": TierRule("default_constant"),
        "tier1": TierRule("default_constant"),
        "tier2": TierRule("default_constant"),
        "tier3": TierRule("alias", alias_key="gpt-mini"),
    },
    # Anthropic: no ``ANTHROPIC_DEFAULT_MODEL`` constant; OC's signal
    # for current models lives entirely in the alias map. The triad
    # opus/sonnet/haiku maps directly to power/workhorse/floor and OC
    # has carried this convention across every release. Haiku isn't in
    # OC's alias map (OC defaults to sonnet for "fast" routing); for
    # tier3 we leave the entry unset and let the static fallback
    # provide claude-haiku-4-5 until OC adds a haiku alias.
    "anthropic": {
        "tier1": TierRule("alias", alias_key="opus"),
        "tier2": TierRule("alias", alias_key="sonnet"),
        # tier3 → static fallback (no OC haiku alias as of v2026.6.1)
    },
    # Google: ``gemini``/``gemini-flash`` aliases map cleanly to
    # workhorse/floor. The flagship constant
    # ``GOOGLE_GEMINI_DEFAULT_MODEL`` exists but currently equals
    # whatever ``gemini`` alias resolves to, so using the alias keeps
    # the resolution path uniform.
    "google": {
        "tier1": TierRule("alias", alias_key="gemini"),
        "tier2": TierRule("alias", alias_key="gemini"),
        "tier3": TierRule("alias", alias_key="gemini-flash"),
    },
    # xAI: no alias entries and no default constant in OC v2026.6.1.
    # Falls through to the static fallback entirely. When OC adds
    # ``grok``/``grok-mini`` aliases (likely next major release), drop
    # them in here.
}


# ── Resolution ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResolvedModel:
    """The result of asking ``resolve_tier_model(provider, tier)``.

    ``source`` names which OC signal produced the model id so the UI
    can show provenance ("from OC v2026.6.1 default_constant"). Useful
    when an operator wonders why "Check Now" suggested X instead of Y.
    """

    model_id: str
    source: Literal["default_constant", "alias", "retirement_redirect"]
    oc_version: str | None
    # When the rule originally resolved to a model that the retirement
    # map flagged as dead, we hop to the replacement and record the
    # detour here so callers can surface both.
    originally_resolved: str | None = None


def _resolve_alias_token(token: str, signals: OcSignals) -> str | None:
    """Turn an alias-target sentinel like ``__alias:opus__`` into a model id.

    The anthropic retirement parser tags every retired claude-* literal
    with one of these sentinels because the actual replacement id lives
    in OC's alias map — different family, different parsing pass. This
    helper does the late substitution.
    """
    if not (token.startswith("__alias:") and token.endswith("__")):
        return token
    alias_name = token[len("__alias:"):-len("__")]
    return signals.aliases.get(alias_name)


def is_retired(
    provider: str, model_id: str, signals: OcSignals | None = None,
) -> tuple[bool, str | None]:
    """Check whether ``model_id`` is in OC's retirement set for ``provider``.

    ``model_id`` may be either the bare name (``gpt-4o``) or the
    provider-qualified form (``openai/gpt-4o``). Returns
    ``(is_retired, replacement_id_or_None)``. The replacement is the
    bare name when known directly; when the retirement parser tagged
    the replacement as ``__alias:opus__`` (anthropic), we resolve the
    alias to the current full id (e.g., ``anthropic/claude-opus-4-8``).
    """
    sig = signals or load_oc_signals()
    prov_map = sig.retirement.get(provider, {})
    if not prov_map:
        return False, None
    bare = model_id.split("/", 1)[-1]
    raw_replacement = prov_map.get(bare)
    if raw_replacement is None:
        return False, None
    # Resolve alias sentinel if present (anthropic case).
    resolved = _resolve_alias_token(raw_replacement, sig)
    if resolved is None:
        # Alias missing from signals — surface the sentinel so the
        # caller at least knows retirement was flagged.
        return True, raw_replacement
    # If the alias resolved to a fully-qualified id (anthropic case),
    # return it as-is. If it's a bare name (openai case), prefix the
    # provider so callers always see ``provider/model``.
    if "/" in resolved:
        return True, resolved
    return True, f"{provider}/{resolved}"


def resolve_tier_model(
    provider: str, tier: str, signals: OcSignals | None = None,
) -> ResolvedModel | None:
    """Resolve the current recommended model for (provider, tier).

    Returns ``None`` when no rule applies or when OC signals are
    missing the data the rule needs. Callers should fall back to the
    static ``model_registry.RECOMMENDED`` dict in that case — None is
    the "we couldn't resolve from OC, use last-known" signal.
    """
    sig = signals or load_oc_signals()
    rule = TIER_POLICY.get(provider, {}).get(tier)
    if rule is None:
        return None

    candidate: str | None = None
    source: str
    if rule.strategy == "default_constant":
        candidate = sig.defaults.get(provider)
        source = "default_constant"
    elif rule.strategy == "alias":
        if rule.alias_key is None:
            return None
        candidate = sig.aliases.get(rule.alias_key)
        source = "alias"
    else:
        return None

    if not candidate:
        return None

    # Defensive: if OC's own signal points us at a name OC's own
    # retirement map then flags, hop to the replacement. Shouldn't
    # happen in practice (OC wouldn't ship inconsistent signals) but
    # the cost of the check is zero and it future-proofs against
    # mid-release inconsistencies during OC's own upgrade windows.
    retired, replacement = is_retired(provider, candidate, sig)
    if retired and replacement:
        return ResolvedModel(
            model_id=replacement,
            source="retirement_redirect",
            oc_version=sig.oc_version,
            originally_resolved=candidate,
        )

    return ResolvedModel(
        model_id=candidate,
        source=source,  # type: ignore[arg-type]
        oc_version=sig.oc_version,
    )


def covered_tiers(provider: str) -> list[str]:
    """Tiers that TIER_POLICY has an OC-driven rule for, in tier order.

    Used by the freshness check to know which (provider, tier) cells
    it can ask the resolver about. Cells outside this set fall through
    to the static RECOMMENDED fallback unchanged.
    """
    rules = TIER_POLICY.get(provider, {})
    order = ("tier0", "tier1", "tier2", "tier3")
    return [t for t in order if t in rules]
