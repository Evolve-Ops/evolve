"""model_catalog.py — reconcile drift between a bot's tier definitions
and its model catalog (`agents.defaults.models`).

Why this exists
===============

Live-discovered 2026-05-28: team_bot_a's catalog has 4 Anthropic models, but
its tier definitions reference Google, xAI, and OpenAI models too —
which are NOT in catalog. At runtime, OpenClaw silently drops any
tier entry that isn't in the catalog (the catalog IS the runtime
whitelist; tiers are routing hints that must intersect with it).

So team_bot_a's tier-1/2/3 say "use google/gemini-2.5-pro" but at runtime the
agent only ever sees the four Anthropic models. The drift is invisible
to operators because nothing surfaces it: the Model Freshness check
only inspects tier membership, never catalog membership.

This module provides:

- `reconcile_catalog(catalog, tiers, ...)` — pure function that
  computes the correct catalog given a set of tier definitions plus
  optional recommended-model seeding for credentialed providers.
  Returns the new catalog and a diff so callers can surface what
  changed.

- `find_catalog_drift(catalog, tiers, ...)` — read-only diff for
  freshness-check advisories. Names every tier entry that isn't in
  catalog and every RECOMMENDED model missing for credentialed
  providers.

Consumers (all in this commit):

  1. `api_admin_config_set_tiers` (web/server.py) — runs reconcile on
     every tier write so the catalog stays in sync. Returns the auto-
     added models in the response so the UI can show what happened.

  2. `check_bot_freshness` (packages/analyzer/model_registry.py) —
     calls `find_catalog_drift` and emits new advisory shapes for
     missing-from-catalog cases.

  3. `evolve-admin reconcile-catalog <bot>` (cli.py) — operator-driven
     one-shot to fix legacy bots that drifted before this enforcement
     existed (team_bot_a, today).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional


_log = logging.getLogger(__name__)


# ── Provider preference for "first available LLM" decisions ──────────────────
#
# Mirrors the same list in provisioning.py so the two reconcile paths
# agree on which provider's models to seed when multiple are
# credentialed. Anthropic first per wizard spec Q4 (Sonnet = default
# forge builder).
_LLM_PROVIDER_PREFERENCE = ["anthropic", "openai", "google", "xai", "moonshot"]


# ── Result types ────────────────────────────────────────────────────────────


@dataclass
class ReconcileResult:
    """Outcome of a `reconcile_catalog` call.

    `new_catalog` is the result you should persist. The diff fields
    tell you what changed so the UI / log can surface it.
    """

    new_catalog: list[dict] = field(default_factory=list)
    added_from_tiers: list[str] = field(default_factory=list)   # model_ids
    added_from_recommended: list[str] = field(default_factory=list)
    # Models added because a MERGED role (defaults ← pod ← bot) resolves to
    # them but they weren't in catalog. Mirrors find_catalog_drift's Type-3
    # (role_resolves_outside_catalog) — this is the half that FIXES the drift
    # the detector flags. The headline case is max → claude-fable-5, which no
    # bot's tiers doc names, so the tier-walk above never adds it and the Max
    # pull dies at OC. Populated only when `resolved_role_models` is passed.
    added_from_roles: list[str] = field(default_factory=list)
    # Tier-referenced models we DIDN'T add because the bot has no API key
    # for that provider. Populated only when `credentialed_providers` is
    # passed to `reconcile_catalog`. Useful for the UI to explain "we
    # cleaned up X, left Y as silent drops until you add a {provider} key."
    skipped_uncredentialed: list[str] = field(default_factory=list)
    unchanged: bool = False


@dataclass
class CatalogDriftFinding:
    """One drift situation surfaced by `find_catalog_drift`.

    Kind:
      - "tier_member_missing": model_id is named in tier `tier` but
        isn't in catalog. Runtime will silently drop it.
      - "recommended_missing": provider has credentials but RECOMMENDED's
        model for `tier` isn't in catalog. (Not necessarily a bug, but
        worth surfacing as a "consider adding" advisory.)
      - "role_resolves_outside_catalog": a MERGED role (defaults ← pod ←
        bot) resolves to model_id, but model_id isn't in this bot's
        catalog. The defining case is ``max`` → ``anthropic/claude-fable-5``
        from the code defaults: no bot's tiers doc names it, so
        ``tier_member_missing`` never fires, yet a Max pull dies at the OC
        layer because the catalog (OC's runtime allowlist) lacks it.
        Here ``tier`` carries the ROLE id (e.g. "max"). (spec §Addendum3.D)
    """

    kind: str            # "tier_member_missing" | "recommended_missing" | "role_resolves_outside_catalog"
    bot_id: str
    tier: str            # legacy tierN key, or a role id for role_resolves_outside_catalog
    provider: str
    model_id: str
    # When kind == "tier_member_missing", whether the model is in
    # RECOMMENDED for that provider+tier (operator likely wanted it)
    # or off-registry (operator chose it explicitly).
    is_recommended: bool = False
    # Whether `reconcile_catalog` would ACTUALLY add this model — i.e. the
    # target bot is credentialed for `provider` (or we don't know the
    # credentialed set, the legacy None path). This mirrors reconcile's
    # credentialed-provider filter EXACTLY: a model reconcile would skip
    # (uncredentialed provider) is flagged provider_credentialed=False here,
    # and one it would add is True. The split matters because "Reconcile
    # catalog" is a NO-OP for an uncredentialed-provider drift (reconcile
    # leaves it as a runtime-graceful silent drop rather than promote it to a
    # fatal "no API key"), so the UI must route those to a missing-credentials
    # fix (copy the provider key, or remove the entry from the tier) instead
    # of the reconcile affordance. Only meaningful for the kinds whose fix is
    # reconcile (tier_member_missing, role_resolves_outside_catalog);
    # recommended_missing is always for a credentialed provider so it stays
    # True. (spec §Addendum 10 §B; consumed by the 12c severity split.)
    provider_credentialed: bool = True


# ── Helpers ─────────────────────────────────────────────────────────────────


def scope_credentialed_to_bot(
    bot_id: str,
    bot_providers_fn,
    pod_providers_fn,
) -> set:
    """Candidate provider set for a per-bot picker / validation surface.

    With a ``bot_id`` whose credentials read cleanly, this is THAT bot's
    credentialed providers (``bot_providers_fn(bot_id)``) — so a per-bot catalog
    offers/accepts only models the bot can actually run. Without a bot_id, OR
    when the bot's auth-profiles are unknown/unreadable/empty, it falls open to
    the pod-wide credentialed union (``pod_providers_fn()``) — never a blank set
    (hiding everything would be worse than offering a model the bot can't run).
    Both inputs are data-derived from auth-profiles; no provider literals.
    """
    if bot_id:
        try:
            bot_providers = bot_providers_fn(bot_id)
        except Exception as exc:  # unreadable creds → fail open to pod set
            _log.warning(
                "picker scope: could not read providers for %s, falling "
                "back to pod-wide set: %s", bot_id, exc,
            )
            bot_providers = set()
        if bot_providers:
            return bot_providers
    return pod_providers_fn()


def _provider_of(model_id: str) -> Optional[str]:
    """Extract provider from a model id like 'anthropic/claude-sonnet-4-6'.

    Mirrors model_registry._provider_of but kept local so this module
    doesn't have to import analyzer/ at module load.
    """
    if not model_id or "/" not in model_id:
        return None
    return model_id.split("/", 1)[0].lower()


def _provider_reconcilable(
    provider: Optional[str],
    credentialed_providers: Optional[set[str]],
) -> bool:
    """Whether `reconcile_catalog` would ACTUALLY add a model for `provider`.

    Mirrors the credentialed-provider filter in reconcile_catalog's invariants
    1 & 3 exactly, so detect and fix agree on a single source of truth: a model
    is reconcilable UNLESS we know the credentialed set AND the model has a real
    provider that isn't in it. The two falls-open cases match reconcile, which
    adds the model in both:

      - `credentialed_providers is None` (legacy callers / no auth read) —
        unfiltered behavior, everything reconcilable.
      - `provider` is falsy (a malformed `model_id` with no ``provider/`` prefix)
        — reconcile's ``if ... prov and ...`` guard is False, so it adds it.

    This is the precise predicate the UI branches on: provider_credentialed=False
    ⇒ "Reconcile catalog" is a no-op, so surface the missing-credentials fix
    instead. (spec §Addendum 10 §B)
    """
    if credentialed_providers is None:
        return True
    if not provider:
        return True
    return provider in credentialed_providers


def _model_in_catalog(catalog: list[dict], model_id: str) -> bool:
    """Check if a model_id appears in a catalog list.

    The catalog stores entries shaped `{"id": "anthropic/...", "provider": "..."}`
    but historical writes (and direct PUTs) have used a plain list of
    strings or dicts with different key names. Be tolerant.
    """
    for entry in catalog:
        if isinstance(entry, str):
            if entry == model_id:
                return True
        elif isinstance(entry, dict):
            if entry.get("id") == model_id or entry.get("model") == model_id:
                return True
    return False


def _all_tier_models(tiers: dict) -> list[str]:
    """Pull every model_id referenced across all tier entries.

    `tiers` is shaped {tier1: {models: [...]}, tier2: {...}, ...}.
    Returns unique model_ids in document order (tier1 first, then tier2, ...).
    """
    seen: set[str] = set()
    result: list[str] = []
    # Iterate in a stable order so repeated calls produce identical lists
    for tier_id in ("tier0", "tier1", "tier2", "tier3"):
        tier_entry = tiers.get(tier_id) or {}
        for m in tier_entry.get("models") or []:
            if isinstance(m, str) and m not in seen:
                seen.add(m)
                result.append(m)
            elif isinstance(m, dict):
                mid = m.get("id") or m.get("model")
                if mid and mid not in seen:
                    seen.add(mid)
                    result.append(mid)
    return result


def _load_recommended() -> dict:
    """Lazy-import model_registry.RECOMMENDED from evolve-analyzer.

    Kept lazy so this module imports even where analyzer isn't
    installed. Returns {} on import failure (the reconcile path that
    doesn't need RECOMMENDED still works).
    """
    try:
        from model_registry import RECOMMENDED  # type: ignore
        return RECOMMENDED
    except Exception as exc:
        _log.warning("model_catalog: could not load RECOMMENDED: %s", exc)
        return {}


# ── Public API ──────────────────────────────────────────────────────────────


def reconcile_catalog(
    catalog: list,
    tiers: dict,
    *,
    credentialed_providers: Optional[set[str]] = None,
    add_recommended_for_credentialed: bool = False,
    resolved_role_models: Optional[dict] = None,
) -> ReconcileResult:
    """Return a catalog that satisfies the invariants:

      1. Every model named in any tier entry IS in the catalog.
         (Required for correctness — OC drops non-catalog tier entries.)

      2. If `add_recommended_for_credentialed` is True, every credentialed
         provider has its RECOMMENDED tier1/2/3 models in the catalog.
         (Helpful default for bots whose catalog is incomplete relative
         to the providers they could be using.)

      3. Every MERGED role (defaults ← pod ← bot) resolves to a model that
         IS in the catalog. The tier-walk in invariant 1 only sees models the
         bot's own tiers doc names — a role resolved purely from the code
         defaults (the ``max`` → ``claude-fable-5`` case) is invisible to it,
         so without this the "Reconcile catalog" action never clears the
         ``role_resolves_outside_catalog`` drift that find_catalog_drift's
         Type-3 pass flags, and the Max pull keeps dying at OC. Detect and fix
         share one source of truth: the caller computes ``resolved_role_models``
         via the same ``primary_bot.resolve_roles_with_provenance`` resolution
         that find_catalog_drift consumes. (spec §Addendum3.D)

    Args:
        catalog: current model catalog (list of {id, provider} dicts
                 or strings — be tolerant of both).
        tiers:   current tier definitions, shape
                 {"tier1": {"models": [...]}, ...}.
        credentialed_providers: providers the bot has auth-profile
                 entries for. Used only when
                 add_recommended_for_credentialed=True.
        add_recommended_for_credentialed: when True, also ensure
                 RECOMMENDED tier1/2/3 models are present in catalog
                 for every credentialed provider. Default False so the
                 hot path (called on every tier write) doesn't make
                 surprise catalog additions.
        resolved_role_models: ``{role: model_id}`` map of the merged
                 (defaults ← pod ← bot) role resolutions, as computed by
                 ``primary_bot.resolve_roles_with_provenance`` (resolvedModel).
                 When set, any resolved model missing from catalog is added
                 (invariant 3). Same credentialed-provider filter as invariant
                 1 applies. Passed in so this module stays free of the analyzer
                 import — and so the same source of truth feeds both the
                 detector and the fix.

    Returns:
        ReconcileResult with `new_catalog` (the catalog you should
        write), `added_from_tiers` + `added_from_recommended` (model_ids
        for diff surfacing), and `unchanged` (True if nothing added).
    """
    # Normalize the incoming catalog into a list of {"id", "provider"} dicts.
    # We keep extra fields if present (some catalog entries have
    # `released`, `display_name`, etc.).
    normalized: list[dict] = []
    for entry in catalog or []:
        if isinstance(entry, str):
            prov = _provider_of(entry)
            normalized.append({"id": entry, "provider": prov} if prov else {"id": entry})
        elif isinstance(entry, dict):
            mid = entry.get("id") or entry.get("model")
            if not mid:
                continue
            new_entry = dict(entry)
            new_entry["id"] = mid
            if "provider" not in new_entry:
                prov = _provider_of(mid)
                if prov:
                    new_entry["provider"] = prov
            normalized.append(new_entry)

    added_from_tiers: list[str] = []
    added_from_recommended: list[str] = []
    added_from_roles: list[str] = []
    skipped_uncredentialed: list[str] = []

    # Invariant 1: every tier model must be in catalog
    #
    # CREDENTIALS-AWARE FILTER (added 2026-05-28 after team_bot_a's 7-drift
    # screenshot): when `credentialed_providers` is set, only auto-add
    # models for providers the bot can actually use. Adding (e.g.)
    # openai/gpt-4o to team_bot_a's catalog when team_bot_a has no OpenAI key would
    # promote a runtime-graceful "silent drop" into a runtime-fatal
    # "no API key for openai" — the exact trap that Seed defaults was
    # built to fix. Skipped models stay in the tier maps (we don't
    # mutate them) and continue to surface as drift advisories so the
    # operator can choose to add the key + reconcile later.
    #
    # When `credentialed_providers` is None (legacy callers), we keep
    # the historical behavior of adding every tier-referenced model
    # so nothing relying on the unfiltered reconcile breaks silently.
    for model_id in _all_tier_models(tiers):
        if _model_in_catalog(normalized, model_id):
            continue
        prov = _provider_of(model_id)
        if (
            credentialed_providers is not None
            and prov
            and prov not in credentialed_providers
        ):
            # Bot has no key for this provider — don't auto-add.
            skipped_uncredentialed.append(model_id)
            continue
        entry = {"id": model_id}
        if prov:
            entry["provider"] = prov
        normalized.append(entry)
        added_from_tiers.append(model_id)

    # Invariant 2 (optional): RECOMMENDED for credentialed providers
    if add_recommended_for_credentialed and credentialed_providers:
        recommended = _load_recommended()
        for provider in sorted(credentialed_providers):
            tier_recs = recommended.get(provider, {})
            for tier_id, entry in tier_recs.items():
                model_id = entry.get("model")
                if not model_id:
                    continue
                if not _model_in_catalog(normalized, model_id):
                    normalized.append({"id": model_id, "provider": provider})
                    added_from_recommended.append(model_id)

    # Invariant 3: MERGED role-resolved models must be in catalog.
    #
    # This is the FIX half of find_catalog_drift's Type-3
    # (role_resolves_outside_catalog) detection — the two share one source of
    # truth (the caller's resolve_roles_with_provenance output) so the
    # "Reconcile catalog" action actually clears the drift the detector flags.
    # The headline case: max → claude-fable-5 is named by no bot's tiers doc,
    # so the tier-walk above never adds it, the drift banner persists, and the
    # Max pull dies at OC's runtime allowlist. Same credentialed-provider
    # filter as invariant 1 — a role resolving to an uncredentialed provider's
    # model is reported as skipped, not silently promoted to a fatal "no key".
    for role, model_id in (resolved_role_models or {}).items():
        if not isinstance(model_id, str) or not model_id:
            continue
        if _model_in_catalog(normalized, model_id):
            continue
        prov = _provider_of(model_id)
        if (
            credentialed_providers is not None
            and prov
            and prov not in credentialed_providers
        ):
            if model_id not in skipped_uncredentialed:
                skipped_uncredentialed.append(model_id)
            continue
        entry = {"id": model_id}
        if prov:
            entry["provider"] = prov
        normalized.append(entry)
        added_from_roles.append(model_id)

    unchanged = not (added_from_tiers or added_from_recommended or added_from_roles)
    return ReconcileResult(
        new_catalog=normalized,
        added_from_tiers=added_from_tiers,
        added_from_recommended=added_from_recommended,
        added_from_roles=added_from_roles,
        skipped_uncredentialed=skipped_uncredentialed,
        unchanged=unchanged,
    )


def find_catalog_drift(
    bot_id: str,
    catalog: list,
    tiers: dict,
    credentialed_providers: Optional[set[str]] = None,
    resolved_role_models: Optional[dict] = None,
) -> list[CatalogDriftFinding]:
    """Return a flat list of drift findings without mutating state.

    Three findings types:
      1. Every tier-referenced model that isn't in catalog
         (runtime-correctness issue — OC silently drops these).
      2. For each credentialed provider, every RECOMMENDED tier
         entry whose model isn't in catalog (advisory — operator
         may want to add the recommended models).
      3. Every MERGED role (defaults ← pod ← bot) whose resolved model
         isn't in catalog (``role_resolves_outside_catalog``). ``tiers``
         only carries models the bot's own doc names; a role resolved
         purely from the code defaults (the ``max`` → ``claude-fable-5``
         case) is invisible to #1. ``resolved_role_models`` is the
         ``{role: model_id}`` map the caller computes via
         ``primary_bot.resolve_roles_with_provenance`` (resolvedModel),
         passed in so this module stays free of the analyzer import.
         (spec §Addendum3.D)

    Designed to feed into Model Freshness advisories so the operator
    sees the same kind of UI for catalog drift as for stale models.
    """
    findings: list[CatalogDriftFinding] = []
    recommended = _load_recommended()

    # Type 1: tier members missing from catalog
    tier_models = _all_tier_models(tiers)
    for model_id in tier_models:
        if _model_in_catalog(catalog, model_id):
            continue
        real_provider = _provider_of(model_id)
        provider = real_provider or "unknown"
        # Credentialed-provider split (spec §Addendum 10 §B): a tier may name a
        # model whose provider the bot has no key for. Reconcile leaves those as
        # runtime-graceful silent drops (adding them would promote the drop to a
        # fatal "no API key"), so the "Reconcile catalog" affordance can't clear
        # them. Flag provider_credentialed=False so the UI offers the
        # missing-credentials fix (copy the key, or remove the tier entry)
        # instead. Same predicate reconcile uses → detect and fix agree.
        prov_credentialed = _provider_reconcilable(
            real_provider, credentialed_providers,
        )
        # Walk tiers to find which tier(s) reference it
        for tier_id in ("tier0", "tier1", "tier2", "tier3"):
            tier_entry = tiers.get(tier_id) or {}
            tier_model_ids = [
                m if isinstance(m, str) else (m.get("id") or m.get("model"))
                for m in (tier_entry.get("models") or [])
            ]
            if model_id in tier_model_ids:
                # Check if this is the RECOMMENDED entry — operator
                # likely intended it, so the fix is "add to catalog"
                # rather than "remove from tier"
                rec_for_tier = (recommended.get(provider, {}) or {}).get(tier_id, {})
                is_rec = rec_for_tier.get("model") == model_id
                findings.append(CatalogDriftFinding(
                    kind="tier_member_missing",
                    bot_id=bot_id,
                    tier=tier_id,
                    provider=provider,
                    model_id=model_id,
                    is_recommended=is_rec,
                    provider_credentialed=prov_credentialed,
                ))
                break  # only report the first tier — same model in
                       # multiple tiers is rare and one finding is enough

    # Type 2: RECOMMENDED missing for credentialed providers
    for provider in sorted(credentialed_providers or set()):
        if provider not in recommended:
            continue
        for tier_id, rec in recommended[provider].items():
            model_id = rec.get("model")
            if not model_id:
                continue
            if not _model_in_catalog(catalog, model_id):
                findings.append(CatalogDriftFinding(
                    kind="recommended_missing",
                    bot_id=bot_id,
                    tier=tier_id,
                    provider=provider,
                    model_id=model_id,
                    is_recommended=True,
                ))

    # Type 3: a MERGED role resolves to a model the catalog lacks. This is the
    # default-coverage gap (spec §Addendum3.D): ``max`` resolves to the
    # code-default ``claude-fable-5``, which no bot's tiers doc names — so the
    # tier_member_missing pass above never sees it — yet a Max pull dies at the
    # OC layer because the catalog (OC's runtime allowlist) doesn't carry it.
    # We surface ONLY models not already flagged by #1 (a tier-named model
    # missing from catalog is the same correctness bug, reported once).
    already_flagged = {f.model_id for f in findings if f.kind == "tier_member_missing"}
    seen_role_models: set[str] = set()
    for role, model_id in (resolved_role_models or {}).items():
        if not isinstance(model_id, str) or not model_id:
            continue
        if model_id in already_flagged or model_id in seen_role_models:
            continue
        if _model_in_catalog(catalog, model_id):
            continue
        seen_role_models.add(model_id)
        real_provider = _provider_of(model_id)
        provider = real_provider or "unknown"
        # Same credentialed-provider parity as Type-1: a role resolving to an
        # uncredentialed provider's model is reconcile-skipped (invariant 3),
        # so flag it for the missing-credentials fix rather than reconcile.
        # In practice availability-aware resolution keeps these credentialed,
        # but the flag stays honest if a resolution slips through.
        findings.append(CatalogDriftFinding(
            kind="role_resolves_outside_catalog",
            bot_id=bot_id,
            tier=role,           # role id, not a legacy tierN key
            provider=provider,
            model_id=model_id,
            is_recommended=False,
            provider_credentialed=_provider_reconcilable(
                real_provider, credentialed_providers,
            ),
        ))

    return findings
