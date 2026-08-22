"""generators.app_suggester.observe — Pod-wide exploratory suggestion.

Reads ``{shared_dir}/applications/{bot_id}/*.json`` for every member bot
to determine the domains each bot's existing apps already cover, then
picks catalog categories from ``catalog.json`` that aren't covered AND
that are grounded in an observation from the Signal store.

Per-pitch dedup: a single catalog category never fans out to multiple
bots in one run. The generator iterates the catalog once and emits at
most one Proposal per category — for the single best-grounded bot.

Grounding floor: a Proposal is only emitted when either
``motivating_signals`` is non-empty OR the technique's confidence is
≥ ``_MIN_UNGROUNDED_CONFIDENCE``. The pure catalog_match technique
sits at confidence 0.6, so without an upstream Signal of type
``app_suggester_gap`` (or similar) scoping the category to a bot,
nothing fires. This is intentional: prior fan-out (one identical
"Daily health check-in for <bot>" per bot, motivating_signals=[],
confidence 0.6) was pure speculation and got shipped as a Proposal,
spamming the operator. See triage 2026-05-25.

Determinism note: when multiple grounded bots tie on signal count, the
first bot in catalog-iteration order wins. Stable order keeps "why did
the system pick this one?" easy to answer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evolve_config import bot_label
from schema.proposal import (
    Investigation,
    Proposal,
    Provenance,
    RiskTag,
    new_proposal_id,
)


GENERATOR_ID = "app_suggester"
DIMENSION = "capabilities"

# Confidence floor for "ungrounded" emissions (motivating_signals empty).
# The catalog_match technique sits at 0.6, well below this — so without
# a Signal pointing at (bot, category), no Proposal is emitted. Raise
# this if a future technique produces high-confidence catalog matches
# (e.g. an LLM-backed bot/catalog fit score).
_MIN_UNGROUNDED_CONFIDENCE = 0.85

# Confidence for the deterministic catalog_match technique. Low on
# purpose — picking from a static catalog with keyword-coverage gating
# is informed guessing, not analysis.
_CATALOG_MATCH_CONFIDENCE = 0.6

# Signal types that, when firing, ground an app_suggester proposal.
# An upstream monitor emits these when it observes (bot, category)
# evidence — e.g. a user mention of a related topic, a recurring gap
# in the bot's activity. Until such a monitor exists no signals match
# and the generator stays silent, which is the correct default.
_GROUNDING_SIGNAL_TYPES = ("app_suggester_gap",)

# Domain keywords used to fingerprint a manifest's coverage. Each
# keyword maps to one or more catalog ``tags`` like ``domain:health``.
# Kept narrow on purpose — false positives ("performance review" →
# domain:fitness) produce bad suggestions; better to under-claim
# coverage and surface one redundant suggestion than miss the bot's
# real footprint.
_DOMAIN_KEYWORDS: dict[str, str] = {
    # ── Original v1 domains (7) ────────────────────────────────────────────
    "health": "domain:health",
    "fitness": "domain:fitness",
    "workout": "domain:fitness",
    "finance": "domain:finance",
    "budget": "domain:finance",
    "expense": "domain:finance",
    "learning": "domain:learning",
    "reading": "domain:learning",
    "course": "domain:learning",
    "study": "domain:learning",
    "creative": "domain:creative",
    "writing": "domain:creative",
    "idea": "domain:creative",
    "journal": "domain:productivity",
    "reflection": "domain:productivity",
    "productivity": "domain:productivity",
    "social": "domain:social",
    "relationship": "domain:social",
    "contact": "domain:social",
    # ── v1.5 expansion (2026-06-05) — 8 new domains ────────────────────────
    # Picked to cover the obvious household / personal use cases v1's
    # narrower set silently dropped. Keyword choice is deliberately
    # conservative: substring matching means short keywords like "cat"
    # or "dog" pull in catastrophe/dogma; we prefer longer, less
    # ambiguous keywords ("puppy", "veterinarian"). False positives on
    # this layer manifest as occasional wrong-domain suggestions that
    # the operator dismisses; the dismiss-signature-based suppression
    # keeps the noise from recurring.
    #
    # food / cooking
    "cooking": "domain:food",
    "recipe": "domain:food",
    "meal": "domain:food",
    "kitchen": "domain:food",
    "baking": "domain:food",
    "ingredient": "domain:food",
    "dinner": "domain:food",
    # home / household
    "chore": "domain:home",
    "cleaning": "domain:home",
    "household": "domain:home",
    "repair": "domain:home",
    "garden": "domain:home",
    "gardening": "domain:home",
    "plant": "domain:home",
    # family / parenting
    "parenting": "domain:family",
    "child": "domain:family",
    "family": "domain:family",
    "baby": "domain:family",
    "school": "domain:family",
    "homework": "domain:family",
    # entertainment / leisure
    "gaming": "domain:entertainment",
    "movie": "domain:entertainment",
    "streaming": "domain:entertainment",
    "hobby": "domain:entertainment",
    # travel
    "travel": "domain:travel",
    "vacation": "domain:travel",
    "flight": "domain:travel",
    "hotel": "domain:travel",
    "itinerary": "domain:travel",
    # music
    "playlist": "domain:music",
    "instrument": "domain:music",
    "concert": "domain:music",
    # work / career — narrower than productivity, captures career-
    # advancement specifically (interview prep, resume, promotion)
    "career": "domain:work",
    "promotion": "domain:work",
    "resume": "domain:work",
    "professional": "domain:work",
    # pets — see substring-matching note above for the keyword choices
    "pet": "domain:pets",
    "puppy": "domain:pets",
    "kitten": "domain:pets",
    "veterinarian": "domain:pets",
}

_CATALOG_PATH = Path(__file__).parent / "catalog.json"


@dataclass
class AppSuggesterContext:
    """Pod-wide run context.

    ``bot_ids`` is the list of member bot IDs from network.json.
    ``shared_dir`` locates per-bot manifests and the Signal store.
    ``catalog_path`` defaults to the bundled ``catalog.json``.

    ``max_per_run`` caps the number of Proposals returned across the
    pod (not per bot). v1 emits one per cycle so the operator isn't
    flooded with exploratory cards.
    """

    bot_ids: list[str]
    shared_dir: Path
    catalog_path: Path = field(default=_CATALOG_PATH)
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    max_per_run: int = 1


def _manifest_dir(shared_dir: Path, bot_id: str) -> Path:
    return shared_dir / "applications" / bot_id


def _load_bot_manifests(shared_dir: Path, bot_id: str) -> list[dict]:
    """Return every manifest dict for this bot, dropping malformed files."""
    out: list[dict] = []
    d = _manifest_dir(shared_dir, bot_id)
    if not d.exists():
        return out
    for f in sorted(d.iterdir()):
        if not f.is_file() or f.suffix != ".json" or f.name.startswith("_") or f.name.startswith("."):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def _extract_covered_domains(
    manifests: list[dict],
    kw_map: dict[str, str] | None = None,
) -> set[str]:
    """Map keyword hits in manifest name + description to domain tags.

    ``kw_map`` lets callers pass a merged (static ∪ dynamic) vocab so
    a bot whose manifests use only dynamic-vocab keywords (e.g. a
    "sourdough-tracker" with no static keyword overlap) still gets
    credit for covering the dynamic domain.

    When ``kw_map`` is None, falls back to the static
    ``_DOMAIN_KEYWORDS`` — preserves pre-Layer-2 behavior for every
    in-tree caller that doesn't yet know about dynamic vocab.
    """
    if kw_map is None:
        kw_map = _DOMAIN_KEYWORDS
    domains: set[str] = set()
    for m in manifests:
        name = (m.get("name") or "").lower()
        desc = (m.get("description") or "").lower()
        purpose = ""
        identity = m.get("identity")
        if isinstance(identity, dict):
            purpose = (identity.get("purpose") or "").lower()
        haystack = " ".join((name, desc, purpose))
        for kw, tag in kw_map.items():
            if kw in haystack:
                domains.add(tag)
    return domains


def _load_catalog(path: Path) -> list[dict]:
    """Read catalog.json. Returns [] if missing or malformed — fail closed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict)]


def _entry_domain_tags(entry: dict) -> set[str]:
    """Pull domain:* tags off a catalog entry."""
    tags = entry.get("tags") or []
    return {t for t in tags if isinstance(t, str) and t.startswith("domain:")}


def _grounding_signals(
    shared_dir: Path, bot_id: str, category: str
) -> list:
    """Return the full Signal objects that ground (bot, category).

    Returning Signal objects (not just IDs) is the post-2026-06-05
    shape: the capability_gap_monitor attaches rich details to each
    Signal (``example_nouns``, ``distinct_sessions``, ``distinct_days``,
    ``engagement_total``, ``objective_fit``) and ``_make_proposal``
    quotes those in the operator-facing pitch. Pre-2026-06-05 Signals
    (or Signals from other producers without details) still work —
    the proposal renderer falls back to today's generic copy.

    Empty list when no firing Signal scopes to this pair. The category
    match is on either ``details.category`` or ``signature`` containing
    the category slug — producers may use either convention.
    """
    try:
        from signals import store as signals_store
    except ImportError:
        return []

    out: list = []
    for sig in signals_store.iter_active(
        shared_dir, bot_id=bot_id, state="firing"
    ):
        if getattr(sig, "type", None) not in _GROUNDING_SIGNAL_TYPES:
            continue
        details = getattr(sig, "details", None) or {}
        sig_category = details.get("category") if isinstance(details, dict) else None
        if sig_category == category:
            out.append(sig)
            continue
        signature = getattr(sig, "signature", "") or ""
        if category and category in signature:
            out.append(sig)
    return out


def _grounding_signal_ids(
    shared_dir: Path, bot_id: str, category: str
) -> list[str]:
    """Backward-compat wrapper around ``_grounding_signals`` that
    returns only IDs. Kept because existing test fixtures and the
    arbiter's lineage scan reference it. New callers should use
    ``_grounding_signals`` so they have access to ``details``."""
    return [s.id for s in _grounding_signals(shared_dir, bot_id, category)]


def _pluck_evidence(grounding_signals: list) -> dict:
    """Squash the details payloads off the firing grounding Signals
    into a single ``evidence`` dict the proposal renderer can quote.

    Multiple Signals can ground the same (bot, category) — e.g. a
    monitor that re-emits per-category every cycle. We merge by
    taking the union of ``example_nouns`` and the max of each numeric
    field, on the principle that the strongest single observation
    is the most defensible to cite.

    Returns empty ``{}`` when no Signal carries the
    ``capability_gap_monitor``-shaped details — the proposal then
    renders without the evidence section (backward-compat with
    pre-2026-06-05 fixtures + any future producer that doesn't ship
    the rich shape)."""
    evidence: dict = {}
    nouns: list[str] = []
    for sig in grounding_signals:
        details = getattr(sig, "details", None) or {}
        if not isinstance(details, dict):
            continue
        for n in details.get("example_nouns") or []:
            if isinstance(n, str) and n not in nouns:
                nouns.append(n)
        for k in ("distinct_sessions", "distinct_days", "engagement_total"):
            v = details.get(k)
            if isinstance(v, (int, float)):
                evidence[k] = max(int(v), evidence.get(k, 0))
        for k in ("objective_fit", "window_days"):
            v = details.get(k)
            if v is not None and k not in evidence:
                evidence[k] = v
    if nouns:
        evidence["example_nouns"] = nouns[:5]
    return evidence


def _make_proposal(
    bot_id: str,
    entry: dict,
    covered_domains: set[str],
    *,
    motivating_signals: list[str] | None = None,
    confidence: float = _CATALOG_MATCH_CONFIDENCE,
    grounding_signals: list | None = None,
) -> Proposal | None:
    """Build a single Investigation Proposal from a catalog entry.

    Returns ``None`` when the grounding floor isn't met:
    ``motivating_signals`` empty AND ``confidence`` below
    ``_MIN_UNGROUNDED_CONFIDENCE``. Callers must treat ``None`` as
    "skip — not enough evidence to emit."

    Post-2026-06-05: when ``grounding_signals`` carries Signals
    emitted by ``capability_gap_monitor`` (with ``details.example_nouns``
    et al.), the operator-facing pitch quotes that evidence. Without
    rich details — pre-spec Signals, fixture-only Signals, or any
    Signal from a producer that doesn't ship the shape — the proposal
    still renders, just without the evidence section.
    """
    sig_ids = list(motivating_signals or [])
    if not sig_ids and confidence < _MIN_UNGROUNDED_CONFIDENCE:
        return None

    category = entry.get("category") or "untitled"
    title = entry.get("title") or category
    description = entry.get("description") or ""
    example_apps = entry.get("example_apps") or []
    entry_domains = sorted(_entry_domain_tags(entry))
    evidence = _pluck_evidence(grounding_signals or [])

    # ── Phase A operator-first content shape ───────────────────────────────
    # admin_surface_summary keeps backward-compat with pre-Phase-A
    # renderers; summary/explanation power the post-Phase-A renderer.
    # See docs/spec-proposal-drafting-protocol-2026-06-04.md.

    bot_name = bot_label(bot_id)
    headline = f"Consider {title} on {bot_name}"
    problem = f"{bot_name}: consider {title.lower()}"

    # Summary (Tier 1): one sentence the operator reads first.
    nouns = evidence.get("example_nouns") or []
    sessions = evidence.get("distinct_sessions") or 0
    days = evidence.get("distinct_days") or 0
    if nouns and sessions and days:
        noun_phrase = ", ".join(f"*{n}*" for n in nouns[:3])
        summary = (
            f"{bot_name} conversations surfaced {noun_phrase} across "
            f"{sessions} session{'s' if sessions != 1 else ''} on "
            f"{days} day{'s' if days != 1 else ''}; no installed app "
            f"covers this domain. Consider {title}."
        )
    elif evidence:
        # Some evidence but missing the rich noun set — quote what we have.
        summary = (
            f"{bot_name} has no installed app covering this domain. "
            f"Consider {title}."
        )
    else:
        # No evidence at all (pre-spec Signal or test fixture).
        covered_summary = (
            f"already covers {', '.join(sorted(d.split(':', 1)[-1] for d in covered_domains))}"
            if covered_domains else
            "has no domain coverage yet"
        )
        summary = f"{bot_name} {covered_summary}; consider {title}."

    # Explanation (Tier 2): the structured body. Sections in priority
    # order: evidence (if any) → catalog entry → example apps → what to do.
    exp_lines: list[str] = []
    if nouns and sessions and days:
        fit = evidence.get("objective_fit", "")
        win = evidence.get("window_days", 30)
        engagement = evidence.get("engagement_total", 0)
        fit_phrase = (
            "The bot's AGENTS.md confirms this domain is in scope."
            if fit == "confirmed"
            else "The bot's AGENTS.md doesn't explicitly mention this domain — "
            "the pattern is strong enough on its own."
            if fit == "neutral"
            else ""
        )
        exp_lines.append(
            f"Over the last {win} days, the bot's user has surfaced "
            f"{noun_phrase} in {sessions} distinct session{'s' if sessions != 1 else ''} "
            f"across {days} day{'s' if days != 1 else ''} "
            f"(engagement total: {engagement})."
        )
        if fit_phrase:
            exp_lines.append("")
            exp_lines.append(fit_phrase)
        exp_lines.append("")
    elif covered_domains:
        exp_lines.append(
            f"{bot_name} already covers "
            f"{', '.join(sorted(d.split(':', 1)[-1] for d in covered_domains))}. "
            f"This category surfaces a domain not in that set."
        )
        exp_lines.append("")
    exp_lines.append(f"**{title}** — {description}")
    if example_apps:
        exp_lines.append("")
        exp_lines.append(
            "Example app names from the catalog: "
            + ", ".join(f"`{n}`" for n in example_apps[:5])
        )
    exp_lines.extend(
        [
            "",
            "**What to do**",
            "- If this fits the bot's purpose, add an app via the "
            "Applications tab. The Pluggables / scanner can also "
            "auto-discover once the workspace has the relevant files.",
            "- If it doesn't fit, dismiss this proposal. The same "
            "category won't be re-suggested for a while.",
            "- The catalog is a starting point, not an opinion — the "
            "operator knows what fits this bot.",
        ]
    )
    explanation = "\n".join(exp_lines)

    # Action body for the Investigation kind — kept in sync with
    # explanation so pre-Phase-A renderers still get the full pitch.
    context = explanation

    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[f"app_suggester:{bot_id}:{category}"],
        provenance=Provenance(
            technique="app_suggester.catalog_match",
            signals={
                "category": category,
                "entry_domains": entry_domains,
                "covered_domains": sorted(covered_domains),
                "example_apps": example_apps[:5],
                "grounding_signal_ids": sig_ids,
                # Stash the evidence so the proposal carries an audit
                # trail of what it cited from the Signals.
                "evidence": evidence,
            },
            confidence=confidence,
        ),
        problem=problem,
        action=Investigation(context=context),
        risk_tag=RiskTag(blast_radius="bot", reversibility="manual", touches=[]),
        claim=None,
        approval_audience="pod_operator",
        urgency="improvement",
        admin_surface_summary=headline[:120],
        motivating_signals=sig_ids,
        # ── Phase A operator-first content ──
        summary=summary,
        explanation=explanation,
        action_label="Open Applications tab",
        manual_path=f"Applications → {bot_name} → Add",
        # Per-category dismiss so suggesting "scheduling" again is fine,
        # but the same category keeps the operator's earlier decision.
        dismiss_signature=f"app_suggester:{category}",
        dismiss_scope="kind",
    )


def observe(ctx: AppSuggesterContext) -> list[Proposal]:
    """Emit up to ``ctx.max_per_run`` exploratory suggestions, pod-wide.

    Algorithm:
    1. For each member bot, load manifests + compute covered domains.
    2. For each catalog entry in catalog order:
       a. Collect candidate bots: those whose covered domains don't
          fully subsume the entry's domain tags.
       b. For each candidate, look up grounding Signals scoped to
          (bot, category).
       c. Pick the single grounded bot with the most signals (ties
          broken by candidate iteration order — i.e. ``bot_ids``
          order). Skip the entry entirely if no candidate has any
          grounding Signals — see ``_MIN_UNGROUNDED_CONFIDENCE``.
       d. Build the Proposal with ``motivating_signals`` populated.
    3. Stop once ``max_per_run`` Proposals have been emitted.

    The arbiter's fingerprint dedup keeps repeat emissions from
    duplicating an already-pending proposal, and the rejection
    cooldown stops re-suggesting recently-dismissed categories.
    """
    catalog = _load_catalog(ctx.catalog_path)
    if not catalog or not ctx.bot_ids:
        return []

    # Merged (static ∪ dynamic) vocab — passed to
    # _extract_covered_domains so manifests using dynamic-vocab
    # keywords get credit for covering their domain. Lazy import
    # because _merged_vocabulary lazy-imports from this module.
    try:
        import _merged_vocabulary as _mv  # type: ignore

        _coverage_kw_map = _mv.effective_keywords(ctx.shared_dir)
    except ImportError:
        _coverage_kw_map = None  # falls back to static inside helper

    # Per-bot manifest + coverage map, computed once.
    coverage: dict[str, set[str]] = {}
    for bot_id in ctx.bot_ids:
        manifests = _load_bot_manifests(ctx.shared_dir, bot_id)
        coverage[bot_id] = _extract_covered_domains(
            manifests, kw_map=_coverage_kw_map
        )

    proposals: list[Proposal] = []
    limit = max(1, ctx.max_per_run)

    for entry in catalog:
        if len(proposals) >= limit:
            break
        category = entry.get("category") or ""
        entry_domains = _entry_domain_tags(entry)

        # Candidate bots are those for whom this entry is uncovered.
        # A multi-domain entry that surfaces one new domain is still
        # uncovered (strict subset check).
        candidates: list[str] = []
        for bot_id in ctx.bot_ids:
            covered = coverage.get(bot_id, set())
            if entry_domains and entry_domains.issubset(covered):
                continue
            candidates.append(bot_id)

        if not candidates:
            continue

        # Pick the best-grounded candidate. "Best" = most firing
        # grounding Signals scoped to (bot, category). Stable by
        # candidate iteration order on ties. We fetch the full
        # Signal objects (not just IDs) so _make_proposal can quote
        # the details payload in its operator-facing pitch — the
        # 2026-06-05 evidence-grounded migration.
        best_bot: str | None = None
        best_grounding: list = []
        best_count = 0
        for bot_id in candidates:
            grounding = _grounding_signals(ctx.shared_dir, bot_id, category)
            if len(grounding) > best_count:
                best_bot = bot_id
                best_grounding = grounding
                best_count = len(grounding)

        if best_bot is None:
            # No grounding anywhere — catalog_match alone is below
            # the confidence floor, so skip the pitch entirely
            # rather than fan it out one-per-bot.
            continue

        proposal = _make_proposal(
            best_bot,
            entry,
            coverage.get(best_bot, set()),
            motivating_signals=[s.id for s in best_grounding],
            grounding_signals=best_grounding,
            confidence=_CATALOG_MATCH_CONFIDENCE,
        )
        if proposal is not None:
            proposals.append(proposal)

    return proposals
