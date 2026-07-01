"""fit_review.gates — the poller's deterministic safety layer (Bite 4).

Spec: docs/spec-fit-reviewer-2026-06-12.md §3.6 (Gate A cite-or-don't + Gate B
honest value) + §3.5 (candidate → Proposal) + §7 (Bite 4).

These are **deterministic, pure-Python gates** — not prompt etiquette. They are
the structural brake (spec §0, §8) that stops the engine from relapsing into the
"138 low-value items" failure mode. The in-bot reflection (Bite 3) *proposes*;
this module *disposes*, trusting nothing the LLM asserted:

  * **cited-quote-exists** (Gate A) — every cited quote is re-verified to exist
    in the bot's transcript at the claimed session. A fabricated quote is
    dropped. The verifier is injected (the poller wires the real transcript
    reader); a candidate needs ≥1 surviving citation.
  * **gallery-pkg-exists** — ``suggested_gallery_pkg_id`` must resolve to a real
    installable gallery app. ``null`` / unknown ⇒ can't emit an ``InstallApp``.
  * **support-reconcile** — the targeting report is recomputed from *current*
    data; the pkg must still be on the shortlist (purpose-aligned need above the
    support floor, not already covered). This re-derives the counts so the LLM
    cannot inflate "69 events" into a higher value tier.
  * **dedup / cooldown** — not already installed; not recently operator-declined
    (the investigation toolkit's ``operator_already_declined``); arbiter
    coalesce/fingerprint folds any overlap with ``app_suggester``.

On PASS the value tier is **recomputed** deterministically (§3.6 Gate B) from the
reconciled support — never the candidate's self-asserted tier — and a single
``InstallApp`` Proposal (``surface="improvement"``, ``altitude=2``) is built.

The module is **I/O-free**: ``catalog``, the recomputed ``targeting_report``, the
``quote_verifier`` and the ``operator_declined`` predicate are all injected, so
every gate is unit-testable without a live shared dir or model. The admin-side
``fit_review_poller`` owns the real readers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Iterable

from fit_review.candidate import CitedEvidence, FitReviewCandidate
from fit_review.targeting import NounRollup, TargetingReport

if TYPE_CHECKING:
    from schema.proposal import ValueTier

# A verifier: (bot_id, session_id, quote) -> True iff the quote is attestable.
QuoteVerifier = Callable[[str, str, str], bool]
# A cooldown predicate: (cause_key) -> True iff the operator already declined it.
DeclinedCheck = Callable[[str], bool]

GENERATOR_ID = "fit_reviewer"
DIMENSION = "capabilities"
ALTITUDE_L2 = 2

# Gate B thresholds (spec §3.6). distinct_sessions on the reconciled,
# purpose-aligned, uncovered domain decides the honest value tier.
VALUE_HIGH_SESSIONS = 8
VALUE_MEDIUM_SESSIONS = 3


# ── Reason codes ─────────────────────────────────────────────────────────────
# A stable taxonomy so the poller can log + route fail modes consistently and
# tests can assert on the exact gate that dropped a candidate.

REASON_OK = "ok"
REASON_NOT_A_SUGGESTION = "not_a_suggestion"   # clean no_candidate — silent, expected
REASON_NO_PKG = "no_pkg"                        # null / unknown pkg_id — no InstallApp possible
REASON_FABRICATED_QUOTE = "fabricated_quote"    # no citation survived re-verification
REASON_ALREADY_INSTALLED = "already_installed"  # the bot already has this app
REASON_SUPPORT_BELOW_FLOOR = "support_below_floor"  # pkg fell off the recomputed shortlist
REASON_VALUE_TOO_LOW = "value_too_low"          # Gate B → "low": not worth a card
REASON_OPERATOR_DECLINED = "operator_declined"  # cooldown — operator said no recently

# Which fail reasons are benign "near-misses" worth a calmer informational
# Observation (spec/task §3: "don't silently vanish a near-miss"). Hallucinations
# (fabricated quotes), cooldowns (already declined), invalid pkgs and clean
# no_candidate records are dropped silently — surfacing them is noise or defeats
# the cooldown. The near-misses are the cases where the bot genuinely had a
# purpose-aligned need that has since thinned or been covered.
_OBSERVATION_WORTHY_REASONS = frozenset(
    {REASON_SUPPORT_BELOW_FLOOR, REASON_VALUE_TOO_LOW}
)


@dataclass
class GateResult:
    """Outcome of running the deterministic gates over one candidate."""

    passed: bool
    reason_code: str
    reason: str
    # Populated on PASS (and partially on a near-miss, for the Observation text):
    pkg_id: str | None = None
    app_name: str | None = None
    primary_domain: str | None = None
    matched_domains: list[str] = field(default_factory=list)
    value_tier: "ValueTier | None" = None
    value_basis: str = ""
    verified_evidence: list[CitedEvidence] = field(default_factory=list)
    distinct_sessions: int = 0
    distinct_days: int = 0

    @property
    def observation_worthy(self) -> bool:
        """A FAIL that should surface as a calmer informational Observation."""
        return (not self.passed) and self.reason_code in _OBSERVATION_WORTHY_REASONS


# ── Helpers ──────────────────────────────────────────────────────────────────


def _catalog_index(catalog: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """{pkg_id → catalog entry} over the real gallery catalog."""
    out: dict[str, dict[str, Any]] = {}
    for entry in catalog or []:
        if not isinstance(entry, dict):
            continue
        pkg_id = str(entry.get("pkg_id") or "")
        if pkg_id:
            out[pkg_id] = entry
    return out


def _rollup_index(report: TargetingReport) -> dict[str, NounRollup]:
    return {r.noun: r for r in report.rollups}


def cause_key_for(bot_id: str, domain: str) -> str:
    """Stable per-(bot, need) cause key — the coalesce/cooldown grain.

    Keyed on the matched *domain* (noun), NOT the run, so re-runs fold into one
    card (spec §3.5 ``coalesce_key = "fit_review:{bot}:{noun}"``) and the
    cooldown check matches across runs.
    """
    return f"fit_review:{bot_id}:{domain}"


def compute_value_tier(
    *, distinct_sessions: int, purpose_aligned: bool, no_current_coverage: bool
) -> "ValueTier":
    """Gate B (spec §3.6) — the honest, deterministic value tier.

        high   if purpose_aligned AND distinct_sessions >= 8 AND no coverage
        medium if purpose_aligned AND distinct_sessions >= 3
        low    otherwise

    Never an LLM number, never a dollar. ``low`` + InstallApp is dropped as not
    worth a card (see :func:`evaluate_candidate`).
    """
    if (
        purpose_aligned
        and distinct_sessions >= VALUE_HIGH_SESSIONS
        and no_current_coverage
    ):
        return "high"
    if purpose_aligned and distinct_sessions >= VALUE_MEDIUM_SESSIONS:
        return "medium"
    return "low"


# ── The gate runner ──────────────────────────────────────────────────────────


def evaluate_candidate(
    candidate: FitReviewCandidate,
    *,
    catalog: list[dict[str, Any]],
    targeting_report: TargetingReport,
    installed_pkg_ids: Iterable[str] = (),
    quote_verifier: QuoteVerifier,
    operator_declined: DeclinedCheck = lambda _ck: False,
) -> GateResult:
    """Run every deterministic gate over ``candidate``. Pure.

    Order is cheapest-and-most-decisive first: structural (is it even a
    suggestion / does the pkg exist), then anti-hallucination (quotes), then the
    substrate-reconcile (support floor / coverage), then value, then cooldown.
    The first failing gate decides the result.
    """
    # ── Gate 0: is this an emission at all? ──────────────────────────────────
    if not candidate.is_suggestion:
        return GateResult(
            passed=False,
            reason_code=REASON_NOT_A_SUGGESTION,
            reason="Runner emitted no_candidate — nothing to gate.",
        )

    # ── Gate: gallery-pkg-exists ─────────────────────────────────────────────
    pkg_id = candidate.pkg_id
    index = _catalog_index(catalog)
    if not pkg_id or pkg_id not in index:
        return GateResult(
            passed=False,
            reason_code=REASON_NO_PKG,
            reason=(
                "suggested_gallery_pkg_id is null or not a real installable "
                f"gallery app ({pkg_id!r}); cannot emit an InstallApp."
            ),
            pkg_id=pkg_id,
        )
    app_name = str(index[pkg_id].get("name") or pkg_id)

    # ── Gate: dedup — already installed ──────────────────────────────────────
    installed = {p for p in installed_pkg_ids if p}
    if pkg_id in installed:
        return GateResult(
            passed=False,
            reason_code=REASON_ALREADY_INSTALLED,
            reason=f"{app_name} ({pkg_id}) is already installed on {candidate.bot_id}.",
            pkg_id=pkg_id,
            app_name=app_name,
        )

    # ── Gate A: cited-quote-exists (anti-hallucination) ──────────────────────
    # Re-verify each citation against the transcript. A candidate must retain at
    # least one attestable quote; a wholly-fabricated citation set is dropped.
    verified = [
        e
        for e in candidate.citable_evidence
        if quote_verifier(candidate.bot_id, e.session_id, e.quote)
    ]
    if not verified:
        return GateResult(
            passed=False,
            reason_code=REASON_FABRICATED_QUOTE,
            reason=(
                "No cited quote survived re-verification against the bot's "
                "transcript (cite-or-don't): dropping rather than shipping an "
                "unverifiable capability claim."
            ),
            pkg_id=pkg_id,
            app_name=app_name,
        )

    # ── Gate: support-reconcile (pkg still on the recomputed shortlist) ───────
    # The targeting report is recomputed from *current* data. If the pkg is no
    # longer shortlisted, the purpose-aligned need either fell below the support
    # floor or is now covered — the LLM's snapshot went stale.
    match = next(
        (m for m in targeting_report.shortlist if m.pkg_id == pkg_id), None
    )
    if match is None:
        return GateResult(
            passed=False,
            reason_code=REASON_SUPPORT_BELOW_FLOOR,
            reason=(
                f"{app_name} ({pkg_id}) is no longer on the recomputed gallery "
                "shortlist — the supporting need fell below the floor "
                "(distinct_sessions ≥ 3, distinct_days ≥ 2) or is now covered."
            ),
            pkg_id=pkg_id,
            app_name=app_name,
        )

    # Re-derive support + alignment from the matched candidate domains. This is
    # the count-reconcile: the value tier is computed from THESE numbers, not the
    # candidate's self-report.
    rollups = _rollup_index(targeting_report)
    matched_domains = list(match.matched_domains)
    matched_rollups = [rollups[d] for d in matched_domains if d in rollups]
    # Lead domain = the matched, above-floor candidate domain with the most
    # distinct sessions (the strongest evidence of recurring need).
    candidate_rollups = [r for r in matched_rollups if r.is_candidate] or matched_rollups
    if not candidate_rollups:
        # Defensive: a shortlisted pkg always traces back to ≥1 candidate domain
        # (that's how the shortlist is built), but never index out of an empty
        # set — treat a torn report as below-floor rather than crashing.
        return GateResult(
            passed=False,
            reason_code=REASON_SUPPORT_BELOW_FLOOR,
            reason=(
                f"{app_name} ({pkg_id}) shortlisted but no candidate domain "
                "rollup backs it — treating as below-floor."
            ),
            pkg_id=pkg_id,
            app_name=app_name,
        )
    lead = max(candidate_rollups, key=lambda r: (r.distinct_sessions, r.distinct_days))
    distinct_sessions = lead.distinct_sessions
    distinct_days = lead.distinct_days
    purpose_aligned = any(r.alignment == "confirmed" for r in candidate_rollups)
    no_current_coverage = not any(r.covered for r in candidate_rollups)

    # ── Gate B: honest value (deterministic, never the LLM's number) ─────────
    value_tier = compute_value_tier(
        distinct_sessions=distinct_sessions,
        purpose_aligned=purpose_aligned,
        no_current_coverage=no_current_coverage,
    )
    value_basis = (
        f"{'purpose-aligned' if purpose_aligned else 'emergent'} need "
        f"'{lead.noun}': {distinct_sessions} distinct sessions over "
        f"{distinct_days} days; "
        f"{'no current coverage' if no_current_coverage else 'partially covered'} "
        f"→ tier={value_tier} (Gate B)"
    )
    if value_tier == "low":
        return GateResult(
            passed=False,
            reason_code=REASON_VALUE_TOO_LOW,
            reason=(
                f"{app_name} ({pkg_id}) computes to value tier 'low' "
                f"({value_basis}); below the bar for a capability card."
            ),
            pkg_id=pkg_id,
            app_name=app_name,
            primary_domain=lead.noun,
            matched_domains=matched_domains,
            value_tier=value_tier,
            value_basis=value_basis,
            distinct_sessions=distinct_sessions,
            distinct_days=distinct_days,
        )

    # ── Gate: cooldown (operator already declined this need) ─────────────────
    ck = cause_key_for(candidate.bot_id, lead.noun)
    if operator_declined(ck):
        return GateResult(
            passed=False,
            reason_code=REASON_OPERATOR_DECLINED,
            reason=(
                f"Operator recently declined the {lead.noun} capability for "
                f"{candidate.bot_id} (cause_key={ck}); suppressing the repeat."
            ),
            pkg_id=pkg_id,
            app_name=app_name,
            primary_domain=lead.noun,
            matched_domains=matched_domains,
        )

    # ── PASS ─────────────────────────────────────────────────────────────────
    return GateResult(
        passed=True,
        reason_code=REASON_OK,
        reason="All deterministic gates passed.",
        pkg_id=pkg_id,
        app_name=app_name,
        primary_domain=lead.noun,
        matched_domains=matched_domains,
        value_tier=value_tier,
        value_basis=value_basis,
        verified_evidence=verified,
        distinct_sessions=distinct_sessions,
        distinct_days=distinct_days,
    )


# ── Proposal construction ────────────────────────────────────────────────────


def _utc_iso(now: datetime | None) -> str:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.isoformat()


def _render_evidence_block(evidence: list[CitedEvidence]) -> str:
    lines = []
    for e in evidence:
        q = e.quote.strip()
        if e.session_id:
            lines.append(f"- “{q}” — session `{e.session_id}`")
        else:
            lines.append(f"- {q}")
    return "\n".join(lines)


def build_install_app_proposal(
    candidate: FitReviewCandidate,
    gate: GateResult,
    *,
    now: datetime | None = None,
):
    """Mint the single ``InstallApp`` Proposal for a PASSing candidate.

    Returns a ``schema.proposal.Proposal`` in ``draft`` status; the poller
    transitions it to ``pending`` and writes it via ``arbiter.store``. Pure —
    no I/O, ``now`` injected for clock-coupling-free tests.

    Surfacing contract (spec §3.5): ``surface="improvement"`` + ``altitude=2`` so
    it reaches the actionable Recommendations Inbox (Pending) via the altitude
    carve-out (``proposal_routing.proposal_surfaces_in_pending_inbox``), even
    though ``app_suggester``-class capability ideas otherwise sit in Observations.
    """
    from schema.proposal import (
        InstallApp,
        Proposal,
        RiskTag,
        ValueEstimate,
        new_proposal_id,
    )
    from schema.provenance import Provenance

    assert gate.passed and gate.pkg_id and gate.primary_domain  # caller contract

    bot_id = candidate.bot_id
    pkg_id = gate.pkg_id
    app_name = gate.app_name or pkg_id
    domain = gate.primary_domain
    ck = cause_key_for(bot_id, domain)

    evidence_block = _render_evidence_block(gate.verified_evidence)
    need_phrase = candidate.recommended_need or f"its recurring {domain} work"

    # Body text goes in Proposal.problem for non-Investigation actions so the
    # operator-first renderer shows it (smarter-generators convention).
    problem = (
        f"{bot_id} is used heavily for {domain} "
        f"({gate.distinct_sessions} distinct sessions over {gate.distinct_days} "
        f"days) with no app supporting it. **{app_name}** would give it a place "
        f"for {need_phrase}.\n\n"
        f"**Why now (cited):**\n{evidence_block}"
    )
    summary = (
        f"{bot_id}'s #1 unmet need is {domain}; {app_name} covers it. "
        f"Evidence: {gate.distinct_sessions} sessions / {gate.distinct_days} days."
    )
    human_title = f"Give {bot_id} {app_name}"

    # evidence_refs: the re-verified citations first, then any value-basis prose
    # the runner attached (kept readable, but the numbers are the recomputed ones).
    evidence_refs = [e.as_ref() for e in gate.verified_evidence]
    for ref in candidate.claimed_evidence_refs:
        if ref and ref not in evidence_refs:
            evidence_refs.append(ref)

    proposal = Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[ck],
        provenance=Provenance(
            technique="fit_reviewer.v1",
            signals={
                "run_id": candidate.run_id,
                "pkg_id": pkg_id,
                "app_name": app_name,
                "recommended_need": candidate.recommended_need,
                "matched_domains": list(gate.matched_domains),
                "primary_domain": domain,
                # cause_key lives here so investigation.proposal_history /
                # operator_already_declined can match this proposal's lineage.
                "root_cause_attribution": {"cause_key": ck},
            },
            confidence=1.0,
        ),
        problem=problem,
        action=InstallApp(app_id=pkg_id, source="gallery"),
        risk_tag=RiskTag(
            blast_radius="bot",
            reversibility="manual",
            touches=["app_install"],
        ),
        # InstallApp binds an already-reviewed package; there is no post-apply
        # metric to verify, so no Claim — never fabricate one to fill the field.
        claim=None,
        approval_audience="bot_primary_user",
        urgency="improvement",
        admin_surface_summary=human_title[:120],
        summary=summary,
        explanation=evidence_block,
        human_title=human_title,
        coalesce_key=ck,
        dismiss_signature=ck,
        motivating_signals=list(candidate.motivating_signals),
        altitude=ALTITUDE_L2,
        value_estimate=ValueEstimate(
            tier=gate.value_tier or "medium",
            basis=gate.value_basis,
            evidence_refs=evidence_refs,
        ),
        surface="improvement",
        created_at=_utc_iso(now),
    )
    return proposal
