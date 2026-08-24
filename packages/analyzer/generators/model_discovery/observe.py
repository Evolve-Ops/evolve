"""generators.model_discovery.observe — discovery-based model freshness.

Spec: internal/spec-model-rungs-and-roles-2026-06-09.md §"Freshness check rework".

Pod-wide (``per_bot=False``) generator. Each daily run:

  1. ``observe_signals(ctx)`` — runs the discovery pipeline ONCE (caching the
     result on ctx), then emits one ``model_discovery`` Signal per discovery
     finding (signature per provider×model so re-fires dedup). Degraded
     providers emit a degraded-mode Signal so silence and "all current" are
     never indistinguishable. This is the SINGLE output of the generator.
  2. ``observe(ctx)`` — returns ``[]``. Discovery no longer authors Proposals
     (spec §Addendum 12, 2026-06-15): a new model is a tactical, provider-
     cadenced advisory ("a model exists; consider adopting it"), nothing the
     bot is running off of broke — it does not earn a Recommendations-queue
     slot, its full card→approve/reject/snooze/dismiss→check-in apparatus, or
     attention taxed on providers' release schedules. Adoption moved to the AI
     Optimization page's Model Freshness card, where the operator adopts a
     discovered model with one click (the gated firing Signal is the card's
     data source; the ``AdoptModel`` applier is its back end). The applier and
     the ``AdoptModel`` builder below are RETAINED — only the front door moved.

The discovery pipeline lives in :mod:`model_discovery`; this module is the
generator-runner glue + Signal authoring.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from schema.proposal import (
    Proposal,
    Provenance,
    AdoptModel,
    RiskTag,
    new_proposal_id,
)

import model_discovery as _md


_log = logging.getLogger(__name__)

GENERATOR_ID = "model_discovery"
DIMENSION = "substrate_health"
PRODUCER = "model_discovery"
SIGNAL_TYPE = "model_discovery"
DEGRADED_SIGNAL_TYPE = "model_discovery_degraded"
# Credentialed provider with NO listing adapter (not in _LISTING_PROVIDERS).
# Its models can't be enumerated, so they never reach the validated picker /
# easy-setup filter. Emitted as an advisory so the gap surfaces on its own
# rather than the provider being silently skipped (the silent-monitor-drift
# lesson — exactly how the xAI gap should have announced itself).
UNCOVERED_SIGNAL_TYPE = "model_provider_no_listing_adapter"

DISMISS_SIGNATURE = "model_discovery:discovery"

# Operator-facing provider display names so the coalesced card reads "New
# models available from xAI", not "…from xai". Keys track the listing-provider
# set (``model_discovery._LISTING_PROVIDERS``) — keep them in sync when a new
# listing adapter is added. Casing follows the same convention as the admin
# UI's ``_PROVIDER_DISPLAY`` (model-catalog.js), though the two maps cover
# different provider sets and are not a single source. Unknown providers fall
# back to the slug (cite-or-don't: never invent a prettier name we can't back).
_PROVIDER_DISPLAY = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "google": "Google",
    "xai": "xAI",
    "deepseek": "DeepSeek",
    "mistral": "Mistral",
}


def _provider_display(provider: str) -> str:
    return _PROVIDER_DISPLAY.get(provider, provider)


# ── Default data readers (production wiring; tests inject) ─────────────────────

def _default_network() -> dict:
    try:
        from evolve_config import load_config
        return load_config()
    except Exception:
        return {}


def _default_bot_users(network: dict) -> list[str]:
    """Macos usernames for every bot, in network.json order (first bot with a
    given provider key wins during pod-wide key resolution)."""
    try:
        from evolve_config import get_bot_user
    except Exception:
        get_bot_user = None  # type: ignore
    users: list[str] = []
    for bot_id in (network.get("bots") or {}).keys():
        if get_bot_user is not None:
            users.append(get_bot_user(bot_id, network))
        else:
            user = (network.get("bots") or {}).get(bot_id, {}).get("user") or bot_id
            users.append(user)
    return users


def _default_bot_configs(network: dict) -> dict[str, dict]:
    """Each bot's full openclaw config (for the legacy tiers fallback when the
    pod hasn't migrated to models.rungs). Best-effort; failures yield {}."""
    try:
        from runtime.agent_runtime import get_runtime
    except Exception:
        return {}
    full_config_get = get_runtime().full_config_get
    out: dict[str, dict] = {}
    for bot_id in (network.get("bots") or {}).keys():
        try:
            cfg = full_config_get(bot_id)
        except Exception:
            cfg = None
        if cfg:
            out[bot_id] = cfg
    return out


@dataclass
class ModelDiscoveryContext:
    """Pod-wide run context.

    All external inputs are injectable so tests run the full pipeline with
    zero HTTP and zero live config reads:
      - ``network`` / ``bot_users`` / ``bot_configs`` override the
        default config readers.
      - ``keys`` pre-resolves {provider: key} (skip auth-profiles reads).
      - ``enumerator`` is the (provider, key) -> ProviderEnumeration callable
        (defaults to the real HTTP fetcher; tests inject a mock).
    """

    bot_id: str | None  # always None — pod-wide
    shared_dir: Path
    network: dict | None = None
    bot_users: list[str] | None = None
    bot_configs: dict[str, dict] | None = None
    keys: dict[str, str] | None = None
    # Full set of providers any bot holds an api_key for (for the
    # credentialed-but-no-adapter gap check). None → resolved from
    # auth-profiles in run_discovery via discover_credentialed_providers.
    credentialed_providers: set[str] | None = None
    enumerator: Callable | None = None
    # Optional pricing-catalog fetcher (Addendum 8 §B). None → run_discovery
    # uses the real HTTP getter; tests inject a stub so the sweep makes zero
    # pricing calls (rather than relying on the suite-wide degrade path).
    pricing_fetcher: Callable | None = None
    # Optional fast-role LLM rationale polish. Default OFF — the pipeline must
    # work fully with it disabled (heuristic rationale stands on its own). A
    # production wiring may set this to a callable(finding) -> str; if it
    # raises or is None, the heuristic rationale is used unchanged.
    rationale_polisher: Callable | None = None
    # Optional capability-aware LLM fit classifier (Addendum 13). Default OFF —
    # the deterministic placement verdict on each finding stands on its own.
    # Production wires this via the generator-runner factory (resolve the
    # fast/judge-role model + key + SDK in generators.model_discovery.fit_llm);
    # tests inject a stub. Signature: (finding) -> dict|None where the dict is
    # ``{verdict, recommended_role, confidence, reason}``. None / low-confidence
    # / a raise → the deterministic verdict is kept (fail-open). It only sharpens
    # placement + authors the reason; it NEVER mutates the catalog.
    fit_classifier: Callable | None = None
    consult_dismissals: bool = True
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Cache so observe_signals() and observe() share ONE discovery run
    # (one listing call per provider per generator pass).
    _result: Any = None


# ── Pipeline (run once, cached) ───────────────────────────────────────────────

def _resolve_result(ctx: ModelDiscoveryContext):
    """Run discovery once and cache on ctx. Returns a DiscoveryResult."""
    if ctx._result is not None:
        return ctx._result
    network = ctx.network if ctx.network is not None else _default_network()
    bot_users = ctx.bot_users if ctx.bot_users is not None else _default_bot_users(network)
    bot_configs = (
        ctx.bot_configs if ctx.bot_configs is not None
        else _default_bot_configs(network)
    )
    result = _md.run_discovery(
        network=network,
        bot_users=bot_users,
        bot_configs=bot_configs,
        shared_dir=Path(ctx.shared_dir),
        keys=ctx.keys,
        credentialed_providers=ctx.credentialed_providers,
        enumerator=ctx.enumerator,
        pricing_fetcher=ctx.pricing_fetcher,
    )
    ctx._result = result
    return result


# ── Quality gates on discovery findings (R5) ──────────────────────────────────
# A live-pod review (handed over by META:reports) found two false-positive
# classes that erode trust in the "new model available" surface:
#   • RECENCY — a model whose provider epoch is well over a year old surfaced as
#     a "new model" advisory (``gpt-4o``: OpenAI's listing reports
#     ``created`` ≈ May 2024, >2 years old).
#   • MODALITY — a non-text-generation model (a Grok VIDEO model) was slotted
#     into an LLM rung that can never route to it.
# Both are filtered HERE, at Signal emission, so a gated finding never becomes a
# ``model_discovery`` Signal and therefore never an AdoptModel Proposal. Dropping
# the Signal also lets the generator-runner sweep auto-resolve any PRE-gate
# firing Signal for the same model — its signature falls out of the kept set,
# so the sweep transitions it to resolved ("detector silent: condition cleared")
# and the stale gpt-4o card self-heals without operator action.
#
# This is the FILTER half of the recency/modality concern. The modality→rung
# MAPPING is a model-tiers concern (cross-aspect seam): if that mapping ever
# needs its own modality check, it should reuse the same data-derived signal —
# see ``_is_non_llm_modality`` below.

# Recency window: a model whose provider-reported epoch is older than this is
# past the point of being a *new*-model advisory. 12 months (not 6): model lines
# refresh roughly annually, and the gate is deliberately conservative about
# SUPPRESSION — it drops only findings with positive evidence of being clearly
# stale (the gpt-4o regression is >2 years old), never a borderline-recent one.
# A model with NO resolvable date is NOT suppressed (see ``_gate_reason``).
_RECENCY_MAX_AGE_DAYS = 365

# Generic, PROVIDER-NEUTRAL media-generation modality tokens that mark a model
# as NOT a text-generation LLM (so NOT a rung-adoption candidate). These EXTEND
# ``model_discovery._NON_CHAT_SUBSTRINGS`` — which already covers
# embedding/audio/image and Google's ``veo`` — with the media tokens that set
# does not yet name, most importantly ``video`` (the Grok video-model
# regression). They are MODALITY words, never provider/model names (the repo's
# no-provider-literals invariant): modality is read from the model id +
# capability flags, never from a provider name.
#
# Cross-aspect seam (model-tiers): when the catalog grows a first-class modality
# field, this gate should read that field instead of substrings, and these
# tokens should fold into the shared ``_NON_CHAT_SUBSTRINGS`` so the validated
# model picker excludes them too.
_NON_LLM_MODALITY_TOKENS = ("video", "speech", "music")


def _model_epoch(evidence: dict) -> datetime | None:
    """Best-effort release/epoch datetime for a discovered model, from the
    listing evidence the generator already carries.

    ``created`` is the unix epoch a provider's ``/v1/models`` listing reports —
    OpenAI-compatible providers (OpenAI, xAI, DeepSeek, Mistral) populate it;
    Anthropic and Google listings don't, so those models legitimately have no
    epoch here. We never FABRICATE a date: a missing / zero / non-numeric /
    out-of-range value returns ``None`` and the caller fails OPEN
    (``_gate_reason``)."""
    created = evidence.get("created")
    if isinstance(created, bool) or not isinstance(created, (int, float)):
        return None
    if created <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(created), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _is_non_llm_modality(evidence: dict) -> bool:
    """True when a discovered model is NOT a text-generation LLM, so it must not
    be offered as a rung-adoption candidate.

    Reuses ``model_discovery._record_is_chat_capable`` (capability flags + the
    shared non-chat substrings) as the base check, then additionally excludes
    the media-generation modalities the shared set does not yet name
    (``_NON_LLM_MODALITY_TOKENS`` — video/…). Modality is derived from the model
    id + capability flags only; no provider-name literal participates."""
    if not _md._record_is_chat_capable(evidence):
        return True
    mid = str(evidence.get("model_id") or "").lower()
    caps = " ".join(str(c).lower() for c in (evidence.get("capabilities") or []))
    blob = f"{mid} {caps}"
    return any(tok in blob for tok in _NON_LLM_MODALITY_TOKENS)


def _gate_reason(evidence: dict, now: datetime) -> str | None:
    """Return a short reason string when a discovery must NOT surface as a "new
    model available" advisory, or ``None`` when it passes both gates.

    Order: MODALITY first (a non-LLM model is never a rung candidate regardless
    of age), then RECENCY. A model with no resolvable date FAILS OPEN — a
    missing date most strongly correlates with a genuinely-new frontier model
    (the Fable case discovery exists to catch: it appears in a provider listing
    before any pricing catalog dates it). Suppressing dateless models would
    re-blind discovery to exactly the new lines it must surface, so we suppress
    ONLY on positive evidence of age."""
    if _is_non_llm_modality(evidence):
        return "non-LLM modality — not a text-generation model, not rung-mappable"
    epoch = _model_epoch(evidence)
    if epoch is not None:
        age_days = (now - epoch).days
        if age_days > _RECENCY_MAX_AGE_DAYS:
            return (
                f"stale — provider epoch {epoch.date().isoformat()} is "
                f"{age_days}d old (> {_RECENCY_MAX_AGE_DAYS}d recency window)"
            )
    return None


# ── Capability-aware fit: optional LLM sharpening of the verdict (Addendum 13) ─
# The finding already carries the DETERMINISTIC verdict (computed in the diff).
# An optional LLM call may SHARPEN it — but only in directions that are safe:
#   • The recommended_rung_slug is ALWAYS computed deterministically (role→rung
#     via the catalog, or a band-derived proposed slug). The LLM never supplies
#     a slug, so a prompt-injected string can't become a persisted rung id.
#   • The recommended_role is validated against the known role set.
#   • A price/observed-derived ``fits_existing`` is AUTHORITATIVE — the cost is
#     ground truth, the LLM may not override it (it would only second-guess a
#     real price). The LLM earns its keep on the cases the deterministic layer is
#     unsure about (unpriced, unknown family — the grok case).
#   • Any miss (no classifier, raise, None, low confidence, invalid role) leaves
#     the deterministic verdict intact. FAIL-OPEN — discovery never depends on
#     the LLM.

_FIT_LLM_MIN_CONFIDENCE = 0.6

# Internal-vocabulary words an operator-facing reason must never contain
# (Addendum 13 vocabulary rule). The system prompt forbids them, but an LLM can
# slip — so if the authored reason carries one, we fall back to the always-clean
# deterministic reason rather than emit jargon. The deterministic reasons never
# use any of these, so the fallback only ever loses LLM flavor, never meaning.
# (Position numbers have no crisp pattern and the reasons never use them, so
# they're left to the system prompt.)
_REASON_JARGON_RE = re.compile(
    r"\b(rungs?|dormant|bands?)\b|new-rung|cost class", re.IGNORECASE
)


def _reason_has_internal_jargon(text: str) -> bool:
    return bool(_REASON_JARGON_RE.search(text or ""))


def _apply_fit_classifier(ctx: ModelDiscoveryContext, finding):
    """Optionally sharpen ``finding``'s deterministic verdict with one LLM call.

    Returns the finding unchanged when no classifier is wired, the call fails /
    returns nothing, or the result doesn't clear the confidence/validation bar.
    """
    classifier = ctx.fit_classifier
    if classifier is None:
        return finding
    try:
        raw = classifier(finding)
    except Exception as exc:  # noqa: BLE001 — fail open on ANY classifier error
        _log.warning(
            "model_discovery: fit classifier raised for %s (%s); keeping "
            "deterministic verdict", finding.qualified_id, type(exc).__name__,
        )
        return finding
    if not raw:
        return finding
    return _merge_llm_verdict(finding, raw)


def _merge_llm_verdict(finding, raw):
    """Merge a validated LLM fit result onto the deterministic finding.

    Conservative + injection-safe (see the module-section note above). Returns a
    NEW finding (via ``dataclasses.replace``) or the original unchanged."""
    import dataclasses

    import model_discovery as _md
    from generators.model_discovery import fit_llm

    # Defensive: a misbehaving classifier could return a truthy non-dict (e.g. a
    # list). Keep the deterministic verdict rather than raise — fail-open all the
    # way down (the production parser only ever returns dict|None).
    if not isinstance(raw, dict):
        return finding

    verdict = str(raw.get("verdict") or "").strip()
    if verdict not in (
        _md.PLACEMENT_FITS_EXISTING, _md.PLACEMENT_NEW_TIER,
        _md.PLACEMENT_MODE_VARIANT, _md.PLACEMENT_SPECIALIST,
        _md.PLACEMENT_CANNOT_PLACE,
    ):
        return finding
    conf_raw = raw.get("confidence")
    if not isinstance(conf_raw, (int, float, str)):
        return finding
    try:
        conf = float(conf_raw)
    except (TypeError, ValueError):
        return finding
    conf = max(0.0, min(1.0, conf))

    # A real-price-derived fit is ground truth — the LLM may not override it.
    det_authoritative = (
        finding.placement_verdict == "fits_existing"
        and (finding.fit_evidence or {}).get("cost_band_source") in ("pricing", "observed")
    )
    if det_authoritative or conf < _FIT_LLM_MIN_CONFIDENCE:
        return finding

    # Author the reason from the LLM, but fall back to the always-clean
    # deterministic reason if it's empty OR slips internal jargon (Addendum 13
    # vocabulary rule — the reason is operator-facing).
    reason = fit_llm.sanitize_reason(raw.get("reason"))
    if not reason or _reason_has_internal_jargon(reason):
        reason = finding.fit_reason

    if verdict == "fits_existing":
        role = str(raw.get("recommended_role") or "").strip().lower()
        if role not in _md.FIT_ROLES:
            return finding  # invalid role → distrust, keep deterministic
        slug = _md.rung_slug_for_role(role)
        if not slug:
            return finding  # role maps to no rung → distrust
        return dataclasses.replace(
            finding, placement_verdict="fits_existing", recommended_role=role,
            recommended_rung_slug=slug, suggested_rung_slug=slug,
            fit_reason=reason, fit_confidence=conf, fit_source="llm",
        )

    if verdict == "new_tier":
        # Slug is deterministic (band-derived), NEVER LLM text. No band to derive
        # one from → fall to cannot_place rather than mint a meaningless tier.
        band = (finding.fit_evidence or {}).get("cost_band")
        slug = _md.proposed_rung_slug(band)
        if not slug or _md.is_placeholder_rung_slug(slug):
            return dataclasses.replace(
                finding, placement_verdict="cannot_place", recommended_role=None,
                recommended_rung_slug=None, suggested_rung_slug="",
                fit_reason=reason, fit_confidence=conf, fit_source="llm",
            )
        return dataclasses.replace(
            finding, placement_verdict="new_tier", recommended_role=None,
            recommended_rung_slug=slug, suggested_rung_slug=slug,
            fit_reason=reason, fit_confidence=conf, fit_source="llm",
        )

    if verdict in (_md.PLACEMENT_MODE_VARIANT, _md.PLACEMENT_SPECIALIST):
        # Off the general ladder — a mode of a placed model, or a domain
        # specialist. No role, no tier: it never routes, so it carries no slug
        # (suggested_rung_slug="" so the applier can never mint a rung for it).
        return dataclasses.replace(
            finding, placement_verdict=verdict, recommended_role=None,
            recommended_rung_slug=None, suggested_rung_slug="",
            fit_reason=reason, fit_confidence=conf, fit_source="llm",
        )

    # cannot_place
    return dataclasses.replace(
        finding, placement_verdict="cannot_place", recommended_role=None,
        recommended_rung_slug=None, suggested_rung_slug="",
        fit_reason=reason, fit_confidence=conf, fit_source="llm",
    )


# ── observe_signals: emit model_discovery Signals ─────────────────────────────

def _stamp_upgrade_first_seen(ctx: ModelDiscoveryContext, result) -> None:
    """Advance the auto-upgrade first-seen ledger from this daily run.

    Spec: internal/spec-model-auto-upgrade-2026-07-30.md §Cadence — the per-item
    ``minVisibleDays`` veto floor needs an item age, and nothing in the system
    recorded one until Phase 3a. This is the writer.

    It lives HERE, on the daily generator run, and deliberately not on the
    admin route that renders the AI Optimization card: a clock advanced by
    page views would never start on a pod whose operator does not open the
    page, which is the same invisibility this whole feature exists to remove.
    The step is pure-read plus one small write — ``result.upgrades`` is
    already computed by the discovery pass this run just made, so there is no
    extra HTTP, no LLM dispatch and no probe, and no new launchd job.
    (``observe_signals`` is also reachable from the on-demand
    ``run_one_generator`` path and hence the canary soak probe; stamp-once
    bounds that to starting a clock at most a day early.)

    Two safety properties, both inherited from the store:

    * **Stamp-once.** A re-detected upgrade keeps its ORIGINAL timestamp, so
      a provider listing blip that hides an upgrade for one run cannot
      restart its veto window when it returns.
    * **No prune on a degraded run.** A provider whose listing call failed
      contributes no upgrade rows, so its entries would look absent. That is
      the same "an empty listing is not 'nothing newer'" rule the discovery
      result already carries — a run that could not look does not get to
      forget.

    Fail-open and loud: a ledger write failure logs a WARNING and leaves the
    run's Signals unaffected. Note what an empty ledger actually means
    downstream — ``model_auto_upgrade._judge`` SKIPS the visibility floor for
    a key it has no stamp for, so an unwritable store is not self-limiting.
    The apply path (P3b) must hold on ``visible_days is None`` rather than
    read it as a pass; that contract lives in ``model_upgrade_store``'s module
    docstring and in spec §Addendum A1.6.
    """
    try:
        import model_upgrade_store as _store

        res = _store.stamp_first_seen(
            Path(ctx.shared_dir),
            _store.upgrade_keys(getattr(result, "upgrades", None) or []),
            now=ctx.now,
            prune_after_days=(
                None if getattr(result, "degraded_providers", None)
                else _store.PRUNE_AFTER_DAYS
            ),
        )
    except Exception as exc:  # noqa: BLE001 — the ledger must never sink a run
        _log.warning(
            "model_discovery: auto-upgrade first-seen stamp failed: %s", exc,
        )
        return
    if res.stamped or res.pruned:
        _log.info(
            "model_discovery: auto-upgrade first-seen ledger — stamped %d, "
            "kept %d, pruned %d",
            len(res.stamped), len(res.kept), len(res.pruned),
        )


def observe_signals(ctx: ModelDiscoveryContext) -> list[dict]:
    """Return signal specs for the generator runner to pass to
    ``signals.store.observe(shared_dir, **spec)``.

    One Signal per discovery finding (signature per provider×model). Plus one
    degraded-mode Signal listing every provider whose listing call failed —
    so a degraded run is loud, never silently "all current".

    Also stamps the auto-upgrade first-seen ledger (see
    :func:`_stamp_upgrade_first_seen`) — this daily run is the veto clock.
    """
    result = _resolve_result(ctx)
    _stamp_upgrade_first_seen(ctx, result)
    specs: list[dict] = []

    for d in result.discoveries:
        # Recency + modality gate (R5). A gated finding never becomes a Signal,
        # so it never becomes an AdoptModel Proposal — and any pre-gate firing
        # Signal for it sweep-resolves once its signature drops out of the kept
        # set. Never silently drop: log the filtered finding so the gate is
        # visible (the silent-monitor-drift lesson applies to our OWN filters).
        gate = _gate_reason(d.evidence, ctx.now)
        if gate is not None:
            _log.info(
                "model_discovery: gated out discovery %s — %s",
                d.qualified_id, gate,
            )
            continue
        # Capability-aware fit verdict (Addendum 13). The finding already carries
        # the DETERMINISTIC verdict from the diff; optionally sharpen it with one
        # cheap LLM fit call. Fail-open: any miss leaves the deterministic
        # verdict intact (see _apply_fit_classifier).
        d = _apply_fit_classifier(ctx, d)
        specs.append({
            "signature": d.signature(),
            "producer": PRODUCER,
            "type": SIGNAL_TYPE,
            # Flavor omitted on purpose: a discovery is advisory ("a new model
            # line exists; consider adopting it"), nothing is broken, so it
            # inherits ``activity`` from producer_severity.PRODUCER_FLAVOR
            # (model_discovery → activity). The degraded / no-adapter signals
            # below override to ``maintenance`` because those *are* fix-it
            # tasks. Reserve the maintenance task inbox for genuine breakage.
            "severity": "info",
            "scope": "pod",
            "category": "hygiene",
            "title": f"New model available: {d.qualified_id}",
            # Operator-facing body speaks plain language (Addendum 13 vocabulary
            # rule): the fit reason, not internal jargon.
            "body": (
                f"{d.qualified_id} is offered by {d.provider} but isn't in your "
                f"line-up yet. {d.fit_reason}"
            ),
            "details": {
                "provider": d.provider,
                "model_id": d.model_id,
                "qualified_id": d.qualified_id,
                "suggested_rung": d.suggested_rung,
                "suggested_rationale": d.suggested_rationale,
                # Structured placement for the AdoptModel action (§Addendum A).
                # NOTE: an unplaceable model carries "" here (NOT the "new-rung"
                # placeholder) — the applier rejects "" so it can't mint a dead
                # rung. The card reads ``recommended_rung_slug`` below.
                "suggested_rung_slug": getattr(d, "suggested_rung_slug", ""),
                "suggested_cost_class": getattr(d, "suggested_cost_class", "medium"),
                "suggested_position": getattr(d, "suggested_position", 0),
                # Cited pricing evidence behind the cost band (Addendum 8 §C):
                # source ∈ {pricing, family-map, heuristic} + the evidence dict
                # ($/token + catalog source, or the matched family). The proposal
                # cites this or says it has none (cite-or-don't-recommend).
                "cost_band_source": getattr(d, "cost_band_source", "heuristic"),
                "cost_band_evidence": getattr(d, "cost_band_evidence", {}),
                # ── Placement verdict (Addendum 13) — the Bite 2 card reads
                # these. ``placement_verdict`` ∈ fits_existing | new_tier |
                # mode_variant | specialist | cannot_place; ``recommended_role``
                # is a real role ONLY for fits_existing (else null);
                # ``recommended_rung_slug`` is the existing rung for
                # fits_existing / a proposed slug for new_tier / null for
                # mode_variant|specialist|cannot_place (NEVER "new-rung");
                # ``fit_reason`` is the plain operator sentence; ``fit_source``
                # is which layer produced it (deterministic|llm).
                "placement_verdict": getattr(d, "placement_verdict", "cannot_place"),
                "recommended_role": getattr(d, "recommended_role", None),
                "recommended_rung_slug": getattr(d, "recommended_rung_slug", None),
                "fit_reason": getattr(d, "fit_reason", ""),
                "fit_confidence": getattr(d, "fit_confidence", 0.0),
                "fit_evidence": getattr(d, "fit_evidence", {}),
                "fit_source": getattr(d, "fit_source", "deterministic"),
                "evidence": d.evidence,
            },
        })

    # Degraded-mode Signal — NEVER let a failed listing read like "current".
    if result.degraded_providers:
        provs = ", ".join(p["provider"] for p in result.degraded_providers)
        reasons = "; ".join(
            f"{p['provider']}: {p['reason']}" for p in result.degraded_providers
        )
        specs.append({
            "signature": "model_discovery:degraded",
            "producer": PRODUCER,
            "type": DEGRADED_SIGNAL_TYPE,
            # Override the producer's activity default: a failed listing call
            # is a real fix-it task (the freshness result is partial), so this
            # one belongs in the maintenance inbox.
            "flavor": "maintenance",
            "severity": "warn",
            "scope": "pod",
            "category": "hygiene",
            "title": f"Model discovery degraded for {provs}",
            "body": (
                f"Discovery skipped for {provs} — the freshness result is "
                f"partial, NOT 'all current'. Reasons: {reasons}. The offline "
                f"RECOMMENDED fallback was used for these providers and cannot "
                f"detect new model lines."
            ),
            "details": {"degraded": result.degraded_providers},
        })

    # Credentialed-but-no-adapter gap Signal — a provider some bot holds a key
    # for, but which Evolve has no listing adapter to enumerate. Its models
    # never reach the validated picker / easy-setup filter. ONE advisory listing
    # every uncovered provider (signature is stable so re-fires dedup; it
    # auto-resolves via sweep when an adapter is added or the key is removed).
    # This is the meta-fix: the gap announces itself instead of a credentialed
    # provider being silently skipped (the silent-monitor-drift lesson — how the
    # xAI gap should have surfaced on its own).
    if result.uncovered_providers:
        provs = ", ".join(result.uncovered_providers)
        specs.append({
            "signature": "model_discovery:no_listing_adapter",
            "producer": PRODUCER,
            "type": UNCOVERED_SIGNAL_TYPE,
            # Override the producer's activity default: "add a listing adapter"
            # is a concrete fix-it task, so this one belongs in the
            # maintenance inbox rather than the advisory triage lane.
            "flavor": "maintenance",
            "severity": "warn",
            "scope": "pod",
            "category": "hygiene",
            "title": f"Credentialed provider has no model-listing adapter: {provs}",
            "body": (
                f"This pod holds an API key for {provs}, but Evolve has no "
                f"model-listing adapter for {'it' if len(result.uncovered_providers) == 1 else 'them'} "
                f"— so {'its' if len(result.uncovered_providers) == 1 else 'their'} "
                f"models are never enumerated and won't appear in the validated "
                f"model picker or the easy-setup LLM-provider filter, even though "
                f"bots can already route to {'it' if len(result.uncovered_providers) == 1 else 'them'}. "
                f"Add a listing adapter (provider endpoint + auth) so "
                f"{'this provider' if len(result.uncovered_providers) == 1 else 'these providers'} "
                f"surface like the rest."
            ),
            "details": {"uncovered_providers": list(result.uncovered_providers)},
        })

    return specs


# ── Retained AdoptModel builder (spec §Addendum 12) ───────────────────────────
# ``_make_discovery_proposal`` is no longer auto-emitted by ``observe`` — the
# generator is signal-only and adoption is driven from the AI Optimization
# Model Freshness card. It is retained as the canonical ``AdoptModel`` proposal
# constructor (cost-band citation + decision-grounding value line + coalesce
# semantics), exercised by the generator's tests and available if the queued
# path is ever re-enabled. The card's adopt route builds the ``AdoptModel``
# *action* directly from the firing Signal's details and drives the same
# ``arbiter.appliers.adopt_model.AdoptModelApplier``.


def _polish_rationale(ctx: ModelDiscoveryContext, finding) -> str:
    """Optional single fast-role LLM polish of the heuristic rationale.

    Default OFF: when no polisher is wired (or it raises), the heuristic
    rationale is returned unchanged — the pipeline is fully functional with
    the LLM call disabled (cost discipline: LLM is escalation, not default)."""
    polisher = ctx.rationale_polisher
    if polisher is None:
        return finding.suggested_rationale
    try:
        polished = polisher(finding)
        if polished and isinstance(polished, str):
            return polished
    except Exception:
        pass
    return finding.suggested_rationale


def _cost_band_cite(cost_class: str, source: str, ev: dict) -> str:
    """One-line cited-pricing string for the cost band (Addendum 8 §C,
    cite-or-don't-recommend). The band is cost class ``cost_class``; ``source``
    says how it was derived and ``ev`` carries the citation:

      - ``pricing``    — exact per-token price from the pricing catalog: cite
        the $/MTok and the catalog source (LiteLLM / models.dev).
      - ``family-map`` — no priced row yet (frontier-lag window): cite the
        matched family that placed the band, and say the catalog hasn't priced
        it.
      - ``heuristic``  — neither a price nor a known family: NO pricing evidence;
        say so plainly so the operator confirms placement against cost manually
        (never a fabricated tier presented as priced)."""
    if source == "pricing":
        in_cost = ev.get("input_cost_per_token")
        out_cost = ev.get("output_cost_per_token")
        cat = ev.get("pricing_source") or "pricing catalog"
        parts = []
        if isinstance(in_cost, (int, float)):
            parts.append(f"input ${in_cost * 1e6:.2f}/MTok")
        if isinstance(out_cost, (int, float)):
            parts.append(f"output ${out_cost * 1e6:.2f}/MTok")
        priced = ", ".join(parts) if parts else "per-token price"
        return (
            f"**Cost band: `{cost_class}`** — computed from pricing ({priced}, "
            f"source: {cat}), placed relative to the catalog's anchor models. "
            f"This is a cited, computed band, not a hand-assigned tier."
        )
    if source == "family-map":
        fam = ev.get("family") or "its family"
        return (
            f"**Cost band: `{cost_class}`** — no pricing-catalog row for this "
            f"model yet (it's newer than the catalogs), so the band comes from "
            f"the curated `{fam}` family mapping. It will recompute from the "
            f"exact price once a pricing catalog lists it."
        )
    return (
        f"**Cost band: `{cost_class}` (UNPRICED — heuristic only)** — no "
        f"pricing-catalog row AND no known family for this model, so the band "
        f"is inferred from naming alone and carries NO pricing evidence. "
        f"Confirm the rung against this model's actual cost before mapping a role."
    )


def _make_discovery_proposal(ctx: ModelDiscoveryContext, sig) -> Proposal | None:
    """Build one AdoptModel Proposal from a firing model_discovery Signal."""
    details = getattr(sig, "details", None) or {}
    provider = details.get("provider")
    qualified = details.get("qualified_id")
    model_id = details.get("model_id")
    if not provider or not qualified or not model_id:
        return None
    suggested_rung = details.get("suggested_rung", "a new rung")
    # Empty (NOT "new-rung"): an unplaceable / off-ladder discovery carries no
    # tier, and the applier rejects an empty/placeholder slug — so this dead
    # path (observe() is signal-only) can never construct the killed literal.
    rung_slug = details.get("suggested_rung_slug") or ""
    cost_class = details.get("suggested_cost_class") or "medium"
    evidence = details.get("evidence") or {}
    # Cited pricing evidence behind the cost band (Addendum 8 §C).
    cost_band_source = details.get("cost_band_source") or "heuristic"
    cost_band_evidence = details.get("cost_band_evidence") or {}

    # Recompute the cost-rank insertion index against the LIVE pod catalog
    # (the diff engine doesn't carry the rungs[] array). Falls back to the
    # signal's stored hint, then 0, if the catalog is unavailable.
    position = details.get("suggested_position", 0)
    try:
        from model_discovery import suggest_rung_position as _pos  # type: ignore

        network = ctx.network if ctx.network is not None else _default_network()
        pod_rungs = ((network or {}).get("models") or {}).get("rungs") or []
        position = _pos(pod_rungs, cost_class)
    except Exception as e:
        # Keep the signal's stored hint (applier re-clamps), but say so —
        # a silent fall-through here would hide a broken catalog read.
        import logging
        logging.getLogger(__name__).warning(
            "model_discovery: live position recompute failed (%s); "
            "using stored hint %s", e, position,
        )

    # Re-derive a polished rationale via the (optional) LLM seam, falling back
    # to the heuristic. We reconstruct a lightweight finding for the polisher.
    rationale = details.get("suggested_rationale") or ""
    if not rationale:
        # The signal body already carries the heuristic rationale; the
        # polisher is a no-op when unwired.
        rationale = getattr(sig, "body", "") or ""

    ctx_lines = []
    cw = evidence.get("context_window")
    mo = evidence.get("max_output_tokens")
    caps = evidence.get("capabilities") or []
    if cw:
        ctx_lines.append(f"- Context window: {cw:,} tokens (from the provider listing)")
    if mo:
        ctx_lines.append(f"- Max output: {mo:,} tokens (from the provider listing)")
    if caps:
        ctx_lines.append(f"- Capability flags: {', '.join(caps)}")
    evidence_block = "\n".join(ctx_lines) if ctx_lines else (
        "- The provider listing carries no context/output metadata for this "
        "model; placement is inferred from family naming alone."
    )

    cost_cite = _cost_band_cite(cost_class, cost_band_source, cost_band_evidence)

    # ── Decision-grounding value line (Bite 3) ──
    # The third rung of the recommendation-legibility contract: join this model
    # × the pod's observed tier usage × cited pricing into ONE terse line (the
    # card face, ``value_line``) plus a richer paragraph (woven into this body /
    # drill-down). Strict cite-or-don't lives entirely in ``value_line`` — an
    # unpriced model says "can't price yet", never a fabricated %. Fail-open: a
    # value-line failure must NEVER sink the proposal (the card just shows no
    # value line) — model_discovery's job is to surface the model regardless.
    value_line_terse: str | None = None
    value_block = ""
    try:
        from generators.model_discovery.value_line import compute_value_line

        vl = compute_value_line(
            provider, model_id,
            shared_dir=Path(ctx.shared_dir),
            provider_display=_provider_display(provider),
            now=ctx.now,
        )
    except Exception:
        vl = None
    if vl is not None:
        value_line_terse = vl.terse
        value_block = f"{vl.detail}\n\n"

    # NOTE: the rich body goes in ``problem`` — the admin UI drops the action
    # body for non-Investigation actions, so AdoptModel must carry its detail
    # here (known gotcha: feedback_proposal_body_in_problem_field).
    body = (
        f"`{qualified}` is listed by {provider}'s live model API but is "
        f"**outside Evolve's default model catalog** and in no rung in this "
        f"pod's config — so no role can route to it and no bot can use it. "
        f"(Evolve's blessed ladder ships in code and never surfaces here; this "
        f"is a model the defaults don't know yet.)\n\n"
        f"**Discovered from:** {provider} `/v1/models` listing (live, this "
        f"run — not a hand-edited list).\n\n"
        f"**Listing evidence:**\n{evidence_block}\n\n"
        f"{cost_cite}\n\n"
        f"{value_block}"
        f"**Suggested placement:** rung `{rung_slug}` "
        f"(cost class {cost_class}, ladder position {position}). "
        f"{suggested_rung}.\n{rationale}\n\n"
        f"**Accepting this proposal adopts the model.** It creates (or extends) "
        f"the `{rung_slug}` rung in `models.rungs` and applies the role-mapping "
        f"and per-day cap you choose on the card. Default is to adopt as a "
        f"dormant catalog entry (no role mapped) — mapping `max`/`power` is a "
        f"separate deliberate choice. The cost band above is the pricing-derived "
        f"placement; confirm it against cost before mapping a role. Dismiss to "
        f"skip; add to the discovery ignore list to never see this model again."
    )

    summary = f"New model available from {provider}: {qualified}"
    # ── Recommendation-legibility contract (internal/design-recommendation-
    #    legibility-2026-06-12.md) ──
    # Coalesce per-provider: every model discovered from one provider folds
    # into ONE card (arbiter.store._coalesce_into_parent keys on coalesce_key;
    # the per-model trigger_observation above is the sub-finding dedup key, so
    # each model stays individually adoptable). xAI shipping six models = one
    # "New models available from xAI" card with six sub-findings, not six fat
    # cards. The human_title is COUNT-AGNOSTIC — the UI's sub-findings badge
    # supplies the live count, which stays correct as models fold in or get
    # adopted out. The heavy maintainer-language body stays in ``problem`` and
    # now correctly lives behind the card's drill-down.
    coalesce_key = f"model_discovery:{provider}"
    human_title = f"New models available from {_provider_display(provider)}"
    explanation = (
        f"Evolve's model-freshness check now *discovers* models by querying "
        f"each provider's live listing, instead of matching against a "
        f"hand-maintained table. {qualified} showed up in {provider}'s "
        f"listing and isn't in any of this pod's rungs.\n\n"
        f"{body}"
    )

    return Proposal(
        id=new_proposal_id(),
        bot_id="<pod>",  # pod-wide sentinel (Proposal.bot_id must be non-empty)
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"model_discovery:{provider}:{model_id}"],
        provenance=Provenance(
            technique="model_discovery.listing_diff",
            signals={
                "provider": provider,
                "qualified_id": qualified,
                "suggested_rung": suggested_rung,
                "suggested_rung_slug": rung_slug,
                "suggested_cost_class": cost_class,
                "suggested_position": position,
                # Cited pricing evidence behind the cost band (Addendum 8 §C).
                "cost_band_source": cost_band_source,
                "cost_band_evidence": cost_band_evidence,
                "evidence": evidence,
            },
            confidence=0.9,
        ),
        problem=body,
        action=AdoptModel(
            provider=provider,
            model_id=model_id,
            rung_slug=rung_slug,
            position=int(position),
            cost_class=cost_class,
            evidence=evidence,
            # role_mapping/cap_per_day stay at the dormant defaults; the
            # /act endpoint patches in the operator's choices at approval.
        ),
        risk_tag=RiskTag(blast_radius="pod", reversibility="manual", touches=["models.rungs"]),
        claim=None,
        approval_audience="pod_operator",
        urgency="improvement",
        admin_surface_summary=summary[:120],
        motivating_signals=[getattr(sig, "id", "")] if getattr(sig, "id", "") else [],
        summary=summary,
        explanation=explanation,
        action_label="Adopt this model",
        manual_path="Settings → Models → rungs",
        dismiss_signature=DISMISS_SIGNATURE,
        dismiss_scope="kind",
        coalesce_key=coalesce_key,
        human_title=human_title,
        value_line=value_line_terse,
    )


def observe(ctx: ModelDiscoveryContext) -> list[Proposal]:
    """Signal-only generator — discovery authors NO Proposals (spec §Addendum 12).

    A newly-discovered model is a tactical, provider-cadenced advisory, not a
    "I changed your config and claim it helped" event — so it does not earn a
    Recommendations-queue slot. The ``model_discovery`` Signal (from
    ``observe_signals``) is the single output; the operator adopts a discovered
    model with one click on the AI Optimization page's Model Freshness card,
    which reads the gated firing Signals and drives the ``AdoptModel`` applier
    directly (see ``evolve_admin.web.model_discovery_adopt``).

    Returning ``[]`` while ``observe_signals`` keeps emitting also performs the
    migration for in-flight proposals: with the charter's
    ``resolves_when_silent: true``, the generator-runner's
    ``sweep_resolve_proposals`` archives any pre-existing pending ``AdoptModel``
    proposal as ``resolved_externally`` on the next run (pod-wide bucket, no
    fingerprint re-emitted) — no one-shot migration script needed.
    """
    return []
