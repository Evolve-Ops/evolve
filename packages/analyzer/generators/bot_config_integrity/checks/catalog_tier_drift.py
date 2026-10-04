"""checks.catalog_tier_drift — tier names model X but X not in catalog.

OpenClaw uses ``agents.defaults.models`` as the runtime model whitelist.
Tier entries naming non-catalog models are silently dropped — the tier
slot becomes invisible at runtime. Discovered with team_bot_a on 2026-05-28:
catalog had only Anthropic, tiers named google/xai/openai too.

This check:
  1. Calls ``evolve_admin.model_catalog.find_catalog_drift`` to identify
     models referenced in tiers but missing from catalog
  2. If any tier-member-missing findings: emit ONE Proposal with action
     ``ReconcileModelCatalog`` listing every missing model_id
  3. Otherwise: no proposal (the bot is in sync)

Correctness invariant — confidence 0.95. Mechanical fix, no semantic
change. Safe to auto-apply at low trust thresholds.
"""

from __future__ import annotations

from typing import Any

from schema.proposal import (
    Proposal,
    Provenance,
    ReconcileModelCatalog,
    RiskTag,
    new_proposal_id,
)

from evolve_config import bot_label


CHECK_NAME = "catalog_tier_drift"

# Phase A.5 dismiss signature. Per-bot via store (one proposal per
# scan rolls up all missing models).
DISMISS_SIGNATURE = f"bot_config_integrity:{CHECK_NAME}"


def _find_drift_safe(bot_id: str, catalog: list, tiers: dict) -> list[Any]:
    """Call ``evolve_admin.model_catalog.find_catalog_drift`` defensively.

    Returns [] on import or call failure. Under-firing is better than
    crashing the generator loop.
    """
    try:
        from evolve_admin.model_catalog import find_catalog_drift
    except Exception:
        return []
    try:
        return find_catalog_drift(bot_id, catalog, tiers)
    except Exception:
        return []


def _is_dismissed(shared_dir, bot_id: str) -> bool:
    """Return True if this check's signature is actively dismissed.

    Fail-open on any read failure.
    """
    try:
        from arbiter.dismissals import is_suppressed
    except ImportError:
        return False
    try:
        return is_suppressed(shared_dir, signature=DISMISS_SIGNATURE, bot_id=bot_id)
    except Exception:
        return False


def _phase_c_content(
    bot_id: str, missing: list[str], by_provider: dict[str, list[str]],
) -> dict:
    """Return Phase C-6 content fields for a tier-drift proposal."""
    bot_name = bot_label(bot_id)
    n = len(missing)
    plural = "s" if n != 1 else ""
    providers = sorted(by_provider.keys())
    providers_str = ", ".join(providers) if providers else "unknown providers"
    summary = (
        f"{bot_name}'s tier definitions reference {n} model{plural} that "
        f"aren't in its `agents.defaults.models` catalog — OpenClaw "
        f"silently drops those tier entries at runtime, so the tier "
        f"slots they occupy are effectively empty. Adding the missing "
        f"entries restores the slots."
    )
    explanation = (
        f"OpenClaw uses `agents.defaults.models` as the runtime "
        f"whitelist of every model the bot is allowed to use. "
        f"Anything not in the catalog gets dropped — even if a tier "
        f"explicitly names it. The result: the tier slot exists in "
        f"config but no model fills it at runtime.\n\n"
        f"Diagnosis. {n} tier-referenced model{plural} aren't in "
        f"this bot's catalog (providers: {providers_str}). The fix "
        f"is mechanical — append the missing entries to the catalog. "
        f"No semantic change; the tiers already wanted these models, "
        f"we're just letting the runtime see them.\n\n"
        f"What this changes. The applier appends the missing models "
        f"to `agents.defaults.models` (idempotent; never removes "
        f"existing entries). A gateway restart picks up the new "
        f"catalog. The operator triggers the restart when ready, so "
        f"the blast radius stays bot-scoped.\n\n"
        f"What could go wrong. If a tier entry is named by mistake "
        f"(typo, a model the bot was never supposed to use), adding "
        f"it to the catalog widens the bot's whitelist without "
        f"intent. Glance at the tier config before approving — if "
        f"the names look wrong, fix the tier instead of the catalog."
    )
    return {
        "summary": summary,
        "explanation": explanation,
        "action_label": "Reconcile catalog",
        "manual_path": f"Settings → Models → {bot_name}",
    }


def _summarize_drift(missing: list[str], bot_id: str) -> tuple[str, str]:
    """Build the (summary, problem) prose pair for the Proposal.

    Grouped by provider so the operator sees the shape of the drift at
    a glance ("team_bot_a needs google/xai/openai catalog entries").
    """
    bot_name = bot_label(bot_id)
    n = len(missing)
    summary = f"Add {n} missing model{'s' if n != 1 else ''} to {bot_name}'s catalog"
    by_provider: dict[str, list[str]] = {}
    for m in missing:
        prov = m.split("/", 1)[0] if "/" in m else "unknown"
        by_provider.setdefault(prov, []).append(m)
    provider_lines = []
    for prov in sorted(by_provider.keys()):
        models = by_provider[prov]
        provider_lines.append(
            f"- **{prov}**: " + ", ".join(f"`{m}`" for m in sorted(models))
        )
    provider_block = "\n".join(provider_lines)
    problem = (
        f"{bot_name}'s tier definitions reference {n} model{'s' if n != 1 else ''} that "
        f"aren't in its `agents.defaults.models` catalog. OpenClaw uses the catalog as "
        f"the runtime whitelist — these tier entries are silently dropped at runtime, "
        f"so the tier slots they occupy are effectively empty.\n\n"
        f"Missing models, by provider:\n\n{provider_block}\n\n"
        f"Applying this proposal appends the missing entries to the catalog "
        f"(idempotent; never removes existing entries). Gateway restart picks up "
        f"the new catalog."
    )
    return summary, problem


def run(ctx, cfg: dict) -> list[Proposal]:
    """Return zero or one Proposal for ``ctx.bot_id`` based on ``cfg``.

    ``ctx`` is a ``BotConfigIntegrityContext`` (dispatcher passes it in).
    ``cfg`` is the dict returned by ``oc_full_config_get`` — the
    dispatcher reads it once per bot and shares across all checks.
    """
    catalog = cfg.get("catalog") or []
    tiers = cfg.get("tiers") or {}

    findings = _find_drift_safe(ctx.bot_id, catalog, tiers)
    # Only act on tier_member_missing (correctness). recommended_missing
    # is the catalog_provider_coverage check's job — different framing,
    # different confidence, separate proposal.
    tier_missing_ids: list[str] = []
    seen: set[str] = set()
    for f in findings:
        if getattr(f, "kind", None) != "tier_member_missing":
            continue
        mid = getattr(f, "model_id", None)
        if not mid or mid in seen:
            continue
        seen.add(mid)
        tier_missing_ids.append(mid)

    if not tier_missing_ids:
        return []

    # Phase A.5 dismiss-suppression gate.
    if _is_dismissed(ctx.shared_dir, ctx.bot_id):
        return []

    summary, problem = _summarize_drift(tier_missing_ids, ctx.bot_id)

    # Phase C-6 content uses the by-provider grouping.
    by_provider: dict[str, list[str]] = {}
    for m in tier_missing_ids:
        prov = m.split("/", 1)[0] if "/" in m else "unknown"
        by_provider.setdefault(prov, []).append(m)
    content = _phase_c_content(ctx.bot_id, tier_missing_ids, by_provider)

    action = ReconcileModelCatalog(
        bot_id=ctx.bot_id,
        add_models=tier_missing_ids,
    )
    proposal = Proposal(
        id=new_proposal_id(),
        bot_id=ctx.bot_id,
        generator_id="bot_config_integrity",
        dimension="safety",
        trigger_observations=[
            f"bot_config_integrity:{CHECK_NAME}:{ctx.bot_id}:"
            f"{len(tier_missing_ids)}-missing"
        ],
        provenance=Provenance(
            technique=f"bot_config_integrity.{CHECK_NAME}",
            signals={
                "bot_id": ctx.bot_id,
                "missing_models": tier_missing_ids,
            },
            confidence=0.95,
        ),
        problem=problem,
        action=action,
        # Catalog-only edit: bot-scoped, auto-revertable (applier
        # snapshots prior catalog), touches model_catalog surface only.
        # Applier does not restart the gateway — operator triggers
        # restart when ready, so blast radius stays bot-scoped.
        risk_tag=RiskTag(
            blast_radius="bot",
            reversibility="auto",
            touches=["model_catalog"],
        ),
        claim=None,
        approval_audience="pod_operator",
        urgency="hygiene",
        admin_surface_summary=summary[:120],
        status="pending",
        # ── Phase C-6 operator-first content (Tier 1 — auto-apply) ──────
        summary=content["summary"],
        explanation=content["explanation"],
        action_label=content["action_label"],
        manual_path=content["manual_path"],
        dismiss_signature=DISMISS_SIGNATURE,
        dismiss_scope="kind",
    )
    return [proposal]
