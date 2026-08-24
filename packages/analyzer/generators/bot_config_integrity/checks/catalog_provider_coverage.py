"""checks.catalog_provider_coverage — bot has provider key but no catalog models.

Detects the "I'm paying for this provider but my bot can't use it"
shape. Example: team_bot_a has Google + xAI + OpenAI keys, but if its catalog
only contained Anthropic models (the pre-#1703 state), the operator
was paying for keys the bot couldn't reach via tier routing.

This check is the "yellow" sibling to catalog_tier_drift's "red":

  - **catalog_tier_drift**: "your config references X but the runtime
    drops it" — a CORRECTNESS bug, 0.95 confidence
  - **catalog_provider_coverage**: "you have a key for X but no X
    models in catalog" — a SUGGESTION, 0.65 confidence

Confidence is meaningfully lower because operators sometimes
deliberately credential a provider for a non-LLM purpose (Brave API
for web search, Google for Calendar) or scope a bot to one provider
even when others are available. Auto-applying this would surprise
operators with new models in their catalog. So the proposal lands in
the queue and the operator decides.

Detection emits ONE proposal per credentialed provider that's missing
from catalog — each one is a separate, individually-approvable choice
("yes, add Google to team_bot_a; no, skip OpenAI on team_bot_a because it's an
operator-curated build").
"""

from __future__ import annotations

from pathlib import Path

from schema.proposal import (
    Proposal,
    Provenance,
    ReconcileModelCatalog,
    RiskTag,
    new_proposal_id,
)


CHECK_NAME = "catalog_provider_coverage"

# Provider IDs we recognize as LLM providers (vs. brave / runway / etc.
# which credential storage but aren't tier-routable). This is the same
# list used by evolve_admin.model_catalog._LLM_PROVIDER_PREFERENCE but
# duplicated here to avoid an admin-side import dependency in the check.
_LLM_PROVIDERS = {"anthropic", "openai", "google", "xai"}


# Phase A.5 dismiss signature — per provider so dismissing google
# coverage doesn't suppress xai coverage on the same bot.
def dismiss_signature_for_provider(provider: str) -> str:
    return f"bot_config_integrity:catalog_provider_coverage:{provider}"


def _is_dismissed(shared_dir, bot_id: str, provider: str) -> bool:
    """Return True if this provider's coverage signature is dismissed.
    Fail-open."""
    try:
        from arbiter.dismissals import is_suppressed
    except ImportError:
        return False
    try:
        return is_suppressed(
            shared_dir,
            signature=dismiss_signature_for_provider(provider),
            bot_id=bot_id,
        )
    except Exception:
        return False


def _phase_c_content(
    bot_id: str, provider: str, rec_models: list[str], degraded: bool = False,
) -> dict:
    n = len(rec_models)
    id_origin = (
        "the offline `RECOMMENDED` table (the live listing was unavailable)"
        if degraded
        else f"{provider}'s live model listing"
    )
    summary = (
        f"{bot_id} has credentials for {provider} but no {provider} "
        f"models in its catalog — the key is paid for but unusable. "
        f"Adding the {n} latest {provider} model{'s' if n != 1 else ''} "
        f"lets tier definitions route to {provider} (you still pick "
        f"which tier uses what)."
    )
    explanation = (
        f"OpenClaw's `agents.defaults.models` block is the runtime "
        f"whitelist — only models on this list can be invoked. Tier "
        f"definitions can name {provider}, but if no {provider} "
        f"models are in the catalog, runtime drops them silently and "
        f"those tier slots are effectively empty.\n\n"
        f"Diagnosis. {bot_id}'s auth profile has a {provider} "
        f"credential (the key is configured), but the catalog has no "
        f"{provider} entries. We don't know whether you intended to "
        f"route a tier to {provider} or just credentialed it for some "
        f"non-LLM reason — this is a lower-confidence finding for "
        f"that reason.\n\n"
        f"What this changes. Approving appends {n} "
        f"{provider} model{'s' if n != 1 else ''} (from {id_origin}) "
        f"to the catalog. The bot's "
        f"tier routing doesn't change — adding to the catalog just "
        f"makes those models available for routing if you choose to "
        f"use them later.\n\n"
        f"What could go wrong. If you credentialed {provider} for a "
        f"non-LLM purpose (rare, but it happens), this proposal is "
        f"noise — dismiss it. If you deliberately scoped this bot to "
        f"one provider, same — dismiss + record an intent. The "
        f"signature is per-provider, so dismissing {provider} "
        f"doesn't silence findings for other providers."
    )
    return {
        "summary": summary,
        "explanation": explanation,
        "action_label": f"Add {provider} models",
        "manual_path": f"Settings → Models → {bot_id}",
    }


def _provider_in_catalog(catalog: list, provider: str) -> bool:
    """Return True if catalog has at least one entry from this provider."""
    for entry in catalog:
        if isinstance(entry, dict):
            mid = entry.get("id") or entry.get("model") or ""
            if "/" in mid and mid.split("/", 1)[0].lower() == provider:
                return True
            if entry.get("provider", "").lower() == provider:
                return True
        elif isinstance(entry, str):
            if "/" in entry and entry.split("/", 1)[0].lower() == provider:
                return True
    return False


def _read_auth_providers_safe(bot_id: str, shared_dir: Path) -> set[str]:
    """List LLM providers this bot has auth-profile entries for.

    Reuses ``evolve_admin.provisioning._read_auth_profile_providers``
    so the auth-file shape tolerance lives in one place. Returns an
    empty set on any failure.
    """
    try:
        from evolve_admin.provisioning import _read_auth_profile_providers
        from evolve_admin.config import load_network
        from evolve_admin.deploy import get_bot_user
    except Exception:
        return set()
    try:
        # Network path discovery: shared_dir is typically
        # {shared_dir}/.. → /Users/Shared/evolve, sibling to the
        # evolve-repo. The canonical network.json lives at
        # shared_dir/network.json on this pod; tests can monkeypatch
        # _read_auth_provider_providers if they care.
        network_path = shared_dir / "network.json"
        if not network_path.exists():
            return set()
        network = load_network(network_path)
        user = get_bot_user(bot_id, network)
        all_providers = _read_auth_profile_providers(user)
        return {p for p in all_providers if p in _LLM_PROVIDERS}
    except Exception:
        return set()


def _load_recommended() -> dict:
    """Lazy-import model_registry.RECOMMENDED. Returns {} on failure."""
    try:
        from model_registry import RECOMMENDED  # type: ignore
        return RECOMMENDED
    except Exception:
        return {}


def _recommended_models_for(provider: str) -> list[str]:
    """Return the tier1/2/3 RECOMMENDED model_ids for a provider.

    Deduped while preserving tier1→tier2→tier3 order, so the first
    entry in the list is the most powerful model (typical "primary"
    candidate).

    Addendum 8 §C: RECOMMENDED is NO LONGER the identity source — this is the
    last-ditch OFFLINE fallback, used only when the listings cache is absent,
    and the caller flags the proposal ``degraded`` when it lands here (it cannot
    name the current model, only the last-hand-edited one).
    """
    rec = _load_recommended().get(provider, {})
    seen: set[str] = set()
    out: list[str] = []
    for tier_id in ("tier1", "tier2", "tier3", "tier0"):
        entry = rec.get(tier_id) or {}
        mid = entry.get("model")
        if mid and mid not in seen:
            seen.add(mid)
            out.append(mid)
    return out


def _listing_models_for(provider: str, shared_dir: Path) -> list[str]:
    """Identity from the LIVE listing, not the hand table (Addendum 8 §A/§C).

    Returns the provider's family-latest, chat-capable model ids (qualified,
    e.g. ``anthropic/claude-opus-4-8``) from the cached ``model-listings.json``
    that discovery refreshes each run. "Family-latest" reuses discovery's own
    ``is_family_latest`` flag (one current id per family — the correctly-spelled
    id the provider itself returns), so we never hand-type or guess a model id.
    Returns ``[]`` when the cache is absent/unreadable or the provider isn't in
    it (the caller then falls back to RECOMMENDED, flagged degraded).
    """
    try:
        from model_discovery import read_listings_cache, _record_is_chat_capable
    except Exception:
        return []
    cache = read_listings_cache(Path(shared_dir))
    if not cache:
        return []
    models = (cache.get("providers") or {}).get(provider) or []
    out: list[str] = []
    seen: set[str] = set()
    for m in models:
        if not isinstance(m, dict):
            continue
        if not m.get("is_family_latest"):
            continue
        if not _record_is_chat_capable(m):
            continue
        qid = m.get("qualified_id") or m.get("model_id")
        if qid and qid not in seen:
            seen.add(qid)
            out.append(qid)
    return out


def _coverage_models_for(provider: str, shared_dir: Path) -> tuple[list[str], bool]:
    """Resolve the candidate models to add for ``provider``, identity-first.

    Returns ``(models, degraded)``:
      - **listing identity (preferred)** — family-latest ids from the live
        listings cache; ``degraded=False``. This is the authoritative,
        correctly-spelled current set (Addendum 8 §A).
      - **RECOMMENDED fallback (degraded)** — only when the listings cache has
        no entry for the provider; ``degraded=True`` so the proposal flags that
        discovery was skipped and the ids may be stale (the silent-monitor-drift
        rule: no "current" from RECOMMENDED alone without a degraded flag).
    """
    listed = _listing_models_for(provider, shared_dir)
    if listed:
        return listed, False
    return _recommended_models_for(provider), True


def _load_pod_models_safe(shared_dir: Path):
    """Read ``network.json::models`` (the pod model layer) next to ``shared_dir``.

    Returns ``None`` on any failure — ``classify_bot_tier_severities`` then folds
    the code defaults alone, which is enough to judge whether a role routes.
    """
    try:
        from evolve_admin.config import load_network  # type: ignore
        np = Path(shared_dir) / "network.json"
        if not np.exists():
            return None
        return (load_network(np) or {}).get("models")
    except Exception:
        return None


def _routable_role_severities(cfg: dict, pod_models, credentialed: set) -> dict:
    """Per-role severity (spec §Addendum 10 §C) under the RUNTIME-routable
    provider set — credentialed AND already present in the catalog whitelist.

    A role classified ``hard_break`` here has NO model OpenClaw can actually
    route to (its rung names only providers that are uncredentialed or absent
    from the catalog). That is the real routing failure this coverage gap can
    cause: a credentialed provider that's *missing from catalog* is exactly the
    fix. Returns ``{role: classification}`` or ``{}`` if the analyzer read path
    is unavailable (then every coverage finding stays the default soft advisory).
    """
    try:
        from primary_bot import classify_bot_tier_severities  # type: ignore
    except Exception:
        return {}
    catalog = cfg.get("catalog") or []
    routable = {p for p in (credentialed or set()) if _provider_in_catalog(catalog, p)}
    try:
        return classify_bot_tier_severities(pod_models, cfg.get("tiers"), routable)
    except Exception:
        return {}


def run(ctx, cfg: dict) -> list[Proposal]:
    """Emit one proposal per credentialed provider missing from catalog.

    Each proposal is independently approvable so operators can selectively
    accept (add Google, skip OpenAI). All proposals share the
    ReconcileModelCatalog action shape but with distinct add_models lists.

    Severity split (spec §Addendum 10 §C): when adding a provider's models would
    give an otherwise-unroutable role a working model, the proposal is reframed
    as a real break (the "maybe deliberately scoped" hypothesis is weak — why
    credential a provider AND reference it in a tier if you didn't want it to
    route?) and nudged one urgency tier up. Confidence stays 0.65 either way, so
    the auto-apply floor (fix_risk-gated) is unchanged.
    """
    catalog = cfg.get("catalog") or []
    credentialed = _read_auth_providers_safe(ctx.bot_id, ctx.shared_dir)
    if not credentialed:
        return []

    # Which roles can't route to ANY catalog-and-credentialed model today, and
    # which providers (if added to catalog) would fix them. Computed once.
    severities = _routable_role_severities(
        cfg, _load_pod_models_safe(ctx.shared_dir), credentialed,
    )

    proposals: list[Proposal] = []
    for provider in sorted(credentialed):
        if _provider_in_catalog(catalog, provider):
            continue  # Already covered — no proposal
        # Identity-first: the live listing is the model-id source; RECOMMENDED is
        # only the degraded offline fallback (Addendum 8 §C).
        rec_models, degraded = _coverage_models_for(provider, ctx.shared_dir)
        if not rec_models:
            continue  # No listing AND no RECOMMENDED entry for this provider (rare)
        # Phase A.5 per-provider dismiss gate.
        if _is_dismissed(ctx.shared_dir, ctx.bot_id, provider):
            continue

        # §C severity: does adding this provider's models give an otherwise-
        # unroutable role a working model? A broken LADDER role means the bot
        # can't do core work (operational). The "deliberately scoped" hypothesis
        # is weak when the gap breaks routing, so it outranks pure hygiene.
        fixed_roles = sorted(
            role for role, c in severities.items()
            if c.get("severity") == "hard_break"
            and provider in (c.get("providers") or [])
        )
        # Soft-preference (2026-06-19): a judge that routes on Standard's own
        # vendor is an ADVISORY (it still routes), not a hard break. Adding a
        # cross-vendor provider (one in its rung but != the doubled-up vendor)
        # upgrades it to an independent cross-vendor check — an improvement, not
        # an operational fix. Tracked separately so the copy stays honest.
        improved_roles = sorted(
            role for role, c in severities.items()
            if c.get("severity") == "advisory"
            and provider in (c.get("providers") or [])
            and provider != c.get("advisory_provider")
        )
        fixed_ladder = [r for r in fixed_roles if r != "judge"]
        if fixed_ladder:
            urgency = "operational_urgent"
        elif fixed_roles or improved_roles:
            urgency = "improvement"
        else:
            urgency = "hygiene"

        # Provenance of the model ids — cited so the operator knows whether the
        # set is the current live listing or the stale offline fallback.
        id_source = (
            "`model_registry.RECOMMENDED` (offline fallback — the live model "
            "listing was unavailable, so these ids may be stale)"
            if degraded
            else f"{provider}'s live `/v1/models` listing (latest in each family)"
        )
        summary = (
            f"Add {provider} models to {ctx.bot_id}'s catalog "
            f"({len(rec_models)} models from "
            f"{'RECOMMENDED — degraded' if degraded else 'the live listing'})"
        )
        content = _phase_c_content(ctx.bot_id, provider, rec_models, degraded)
        degraded_note = (
            f"\n\n⚠️ The live {provider} model listing was unavailable this run, "
            f"so these ids come from the offline `RECOMMENDED` table and may be "
            f"stale. Re-run discovery before adopting if you can."
            if degraded else ""
        )
        if fixed_roles:
            role_names = ", ".join(fixed_roles)
            closing = (
                f"\n\n⚠️ **This isn't optional for {ctx.bot_id}.** Right now the "
                f"{role_names} role{'s' if len(fixed_roles) != 1 else ''} "
                f"{'have' if len(fixed_roles) != 1 else 'has'} no model OpenClaw "
                f"can route to — every model in the chain is from a provider that's "
                f"uncredentialed or absent from the catalog (spec §Addendum 10 §C "
                f"hard break). Adding {provider}'s models is the fix."
            )
        elif improved_roles:
            role_names = ", ".join(improved_roles)
            closing = (
                f"\n\nRecommended (not required): the {role_names} "
                f"role{'s' if len(improved_roles) != 1 else ''} currently "
                f"{'route' if len(improved_roles) != 1 else 'routes'} on the same "
                f"vendor as Standard. {provider} is a different provider, so adding "
                f"its models lets {ctx.bot_id} run independent cross-vendor checks. "
                f"It already routes today — this is a quality improvement, not a fix."
            )
        else:
            closing = (
                f"\n\nLower-confidence proposal: you may have deliberately scoped this "
                f"bot to one provider, or credentialed {provider} for a non-LLM "
                f"purpose. Reject if so."
            )
        problem = (
            f"{ctx.bot_id} has an auth-profile credential for **{provider}** but no "
            f"{provider} models in `agents.defaults.models`. The bot is paying for the "
            f"key without being able to reach the provider via tier routing.\n\n"
            f"Adding these models doesn't change which model the bot's tiers select — "
            f"the catalog is just the runtime whitelist of what's allowed. To actually "
            f"route a tier to {provider}, edit the tier definition after this lands.\n\n"
            f"Models that would be added (from {id_source}):\n\n"
            + "\n".join(f"- `{m}`" for m in rec_models)
            + degraded_note
            + closing
        )
        action = ReconcileModelCatalog(
            bot_id=ctx.bot_id,
            add_models=rec_models,
        )
        proposal = Proposal(
            id=new_proposal_id(),
            bot_id=ctx.bot_id,
            generator_id="bot_config_integrity",
            dimension="safety",
            trigger_observations=[
                f"bot_config_integrity:{CHECK_NAME}:{ctx.bot_id}:{provider}"
            ],
            provenance=Provenance(
                technique=f"bot_config_integrity.{CHECK_NAME}",
                signals={
                    "bot_id": ctx.bot_id,
                    "provider": provider,
                    "recommended_models": rec_models,
                    # Identity provenance (Addendum 8 §C): "listing" = current
                    # live ids; "recommended_degraded" = stale offline fallback.
                    "model_id_source": (
                        "recommended_degraded" if degraded else "listing"
                    ),
                    # §C severity context: "hard_break" if this coverage gap
                    # leaves a role with no routable model; "advisory" if it only
                    # upgrades a same-vendor judge to a cross-vendor check (soft
                    # preference, 2026-06-19); else "soft" hygiene. Plus the roles
                    # each tier fixes / improves.
                    "coverage_severity": (
                        "hard_break" if fixed_roles
                        else "advisory" if improved_roles
                        else "soft"
                    ),
                    "fixes_hard_break_roles": fixed_roles,
                    "improves_advisory_roles": improved_roles,
                },
                # Meaningfully lower than tier_drift — operators may have
                # deliberately scoped catalogs. Auto-apply thresholds
                # should be tuned to NOT auto-apply at this confidence.
                confidence=0.65,
            ),
            problem=problem,
            action=action,
            risk_tag=RiskTag(
                blast_radius="bot",
                reversibility="auto",
                touches=["model_catalog"],
            ),
            claim=None,
            approval_audience="pod_operator",
            urgency=urgency,
            admin_surface_summary=summary[:120],
            status="pending",
            # ── Phase C-6 operator-first content (Tier 1 — auto-apply) ──
            summary=content["summary"],
            explanation=content["explanation"],
            action_label=content["action_label"],
            manual_path=content["manual_path"],
            dismiss_signature=dismiss_signature_for_provider(provider),
            dismiss_scope="kind",
        )
        proposals.append(proposal)
    return proposals
