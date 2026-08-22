"""fit_review.candidate — parse + normalize an in-bot reflection candidate.

Spec: docs/spec-fit-reviewer-2026-06-12.md §3.5 + §7 (Bite 4).

Bite 3 (the in-bot reflection runner) writes **one candidate JSON per (run,
bot)** to ``{shared_dir}/fit_review/outbox/<run_id>/<bot_id>.json``. This module
is the **tolerant reader** between the two bites: it accepts BOTH the current
candidate-contract field names AND the spec §3.5 field names as aliases, so the
poller (Bite 4) does not wedge if Bite-3's exact field names drift on rebase.

The alias map is the *single* place that knowledge lives — the poller and the
gates work only off the normalized :class:`FitReviewCandidate`. When Bite 3
lands, reconcile any field-name delta HERE (and nowhere else).

Field aliases (contract name ↔ spec §3.5 name):

    bot_id                    ↔ bot_id
    suggested_gallery_pkg_id  ↔ app_pkg_id
    recommended_need          ↔ archetype_lens (free-text need framing)
    cited_evidence[{quote,    ↔ evidence[str]  (spec carried prose lines; the
        session_id, ts}]         contract carries structured, attestable quotes)
    value_estimate{tier,      ↔ value_tier / value_basis
        basis, evidence_refs}
    support{distinct_sessions,↔ targeting_support{distinct_sessions,
        days}                     distinct_days, frustration_share}
    targeting_decision        ↔ decision ("suggest" | "no_candidate")
    run_id / created_at       ↔ (n/a — the poller stamps run_id from the path)

**Runner contract — emit STRUCTURED citations, not prose.** ``cited_evidence``
MUST be the structured shape ``[{quote, session_id, ts?}]`` whose ``session_id``s
exist in the bot's turn store. The parser also tolerates the spec §3.5 prose form
(``evidence: [str]``) so a Bite-3 field-name drift on rebase doesn't wedge the
poller — but a prose line carries no session, so it is never *citable*
(:attr:`CitedEvidence.is_citable` is False) and a prose-only candidate **silently
fails Gate A** (cite-or-don't, §3.6 Gate A). That is correct-by-design —
un-attestable prose must not clear the anti-hallucination floor — but it means the
Bite-3 runner must emit session-attested structured quotes or *every* one of its
suggestions is dropped. See :func:`_parse_cited_evidence` below.

This module is **pure** — no I/O. ``parse_candidate`` takes the already-loaded
dict; the poller owns the file read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Targeting-report decision codes (fit_review.targeting) that mean "the runner
# found something worth emitting". Anything else (no_candidate / no_purpose /
# gap_no_gallery_match) is a deliberate non-emission and is treated as
# decision="no_candidate" here.
_SUGGEST_DECISIONS = frozenset({"suggest", "targets_found"})


@dataclass
class CitedEvidence:
    """One transcript-cited evidence line — the citation that Gate A re-verifies.

    ``quote`` is the verbatim user/bot line the reflection cited; ``session_id``
    is the session the bot attests it came from (re-checkable against the
    transcript by the poller's quote verifier); ``ts`` is the optional
    timestamp. A line with an empty ``quote`` carries no citation and never
    counts toward the cite-or-don't floor.
    """

    quote: str
    session_id: str
    ts: str | None = None

    @property
    def is_citable(self) -> bool:
        return bool(self.quote.strip()) and bool(self.session_id.strip())

    def as_ref(self) -> str:
        """Compact ``session_id: quote`` reference for ValueEstimate.evidence_refs."""
        q = self.quote.strip()
        return f"{self.session_id}: {q}" if self.session_id else q


@dataclass
class FitReviewCandidate:
    """A normalized in-bot reflection candidate, ready for the poller's gates.

    Self-reported fields (``claimed_*``) are exactly that — the in-bot LLM's
    assertions. The poller NEVER trusts them: Gate A re-verifies the quotes,
    support-reconcile re-derives the counts, and the value tier is recomputed
    deterministically (§3.6 Gate B). They are kept only so a near-miss
    Observation can explain what the runner thought it found.
    """

    bot_id: str
    decision: str  # normalized: "suggest" | "no_candidate"
    # identity: see applications.app_identity.resolve_app_id — the gallery
    # catalog key the in-bot LLM picked off the shortlist, parsed out of an
    # outbox record. There is no manifest in scope to resolve: the app is by
    # construction one the bot does NOT have installed.
    pkg_id: str | None  # suggested_gallery_pkg_id (real gallery id) or None
    recommended_need: str
    archetype: str | None
    cited_evidence: list[CitedEvidence]
    claimed_value_tier: str | None
    claimed_value_basis: str
    claimed_evidence_refs: list[str]
    claimed_support: dict[str, Any]
    motivating_signals: list[str]
    run_id: str
    created_at: str | None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_suggestion(self) -> bool:
        """True iff this candidate asks for an emission (vs. a clean no_candidate)."""
        return self.decision == "suggest"

    @property
    def citable_evidence(self) -> list[CitedEvidence]:
        return [e for e in self.cited_evidence if e.is_citable]


def _first(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Return the first present, non-None value among ``keys``."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _coerce_str(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def _parse_cited_evidence(raw: Any) -> list[CitedEvidence]:
    """Normalize the cited-evidence list, accepting both the structured
    contract shape (``[{quote, session_id, ts}]``) and the spec §3.5 shape
    (``evidence: [str]`` — prose lines with no attestable session).

    A bare-string evidence line becomes a CitedEvidence with an empty
    ``session_id`` (so it is NOT citable and never satisfies Gate A on its
    own — prose without a session can't be re-verified). This keeps the
    spec-shape readable in a near-miss Observation without ever letting an
    un-attestable line clear the anti-hallucination floor.
    """
    out: list[CitedEvidence] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, dict):
            quote = _coerce_str(_first(item, "quote", "text", default=""))
            session_id = _coerce_str(_first(item, "session_id", "session", default=""))
            ts = _first(item, "ts", "timestamp", "timestamp_start")
            out.append(
                CitedEvidence(
                    quote=quote,
                    session_id=session_id,
                    ts=_coerce_str(ts) or None,
                )
            )
        elif isinstance(item, str):
            out.append(CitedEvidence(quote=item.strip(), session_id="", ts=None))
    return out


def _normalize_decision(raw: dict[str, Any], pkg_id: str | None) -> str:
    """Resolve a normalized ``"suggest" | "no_candidate"`` decision.

    Preference: an explicit ``decision`` / ``targeting_decision`` field, else
    inferred from whether a pkg_id + citable evidence are present.
    """
    explicit = _coerce_str(
        _first(raw, "decision", "targeting_decision", default="")
    ).lower()
    if explicit:
        return "suggest" if explicit in _SUGGEST_DECISIONS else "no_candidate"
    # No explicit decision — infer. A pkg_id is the minimum to be an emission.
    return "suggest" if pkg_id else "no_candidate"


def _parse_value_estimate(raw: dict[str, Any]) -> tuple[str | None, str, list[str]]:
    """Return (claimed_tier, claimed_basis, claimed_evidence_refs).

    Accepts the nested contract shape (``value_estimate: {tier, basis,
    evidence_refs}``) and the flat spec §3.5 shape (``value_tier`` /
    ``value_basis``).
    """
    ve = raw.get("value_estimate")
    if isinstance(ve, dict):
        tier = _coerce_str(ve.get("tier")) or None
        basis = _coerce_str(ve.get("basis"))
        refs = [str(r) for r in (ve.get("evidence_refs") or []) if r]
        return tier, basis, refs
    tier = _coerce_str(_first(raw, "value_tier", default="")) or None
    basis = _coerce_str(_first(raw, "value_basis", default=""))
    # spec §3.5 carried prose evidence lines in ``evidence``; reuse them as refs.
    refs = [str(r) for r in (raw.get("evidence") or []) if isinstance(r, str)]
    return tier, basis, refs


def parse_candidate(
    data: dict[str, Any],
    *,
    run_id: str = "",
    bot_id: str = "",
) -> FitReviewCandidate:
    """Normalize a raw outbox candidate dict into a :class:`FitReviewCandidate`.

    ``run_id`` / ``bot_id`` are the values the poller derived from the outbox
    path (``outbox/<run_id>/<bot_id>.json``); they are used as fallbacks when
    the record itself omits them, so the path is authoritative for identity.
    """
    if not isinstance(data, dict):
        data = {}

    resolved_bot = _coerce_str(_first(data, "bot_id", default="")) or bot_id
    resolved_run = _coerce_str(_first(data, "run_id", default="")) or run_id

    pkg_id = _coerce_str(
        _first(data, "suggested_gallery_pkg_id", "app_pkg_id", "pkg_id", default="")
    ) or None

    cited = _parse_cited_evidence(
        _first(data, "cited_evidence", "evidence", default=[])
    )
    decision = _normalize_decision(data, pkg_id)
    tier, basis, refs = _parse_value_estimate(data)

    support = _first(data, "support", "targeting_support", default={}) or {}
    if not isinstance(support, dict):
        support = {}

    motivating = [
        str(s) for s in (data.get("motivating_signals") or []) if s
    ]

    return FitReviewCandidate(
        bot_id=resolved_bot,
        decision=decision,
        pkg_id=pkg_id,
        recommended_need=_coerce_str(
            _first(data, "recommended_need", "archetype_lens", default="")
        ),
        archetype=_coerce_str(_first(data, "archetype", default="")) or None,
        cited_evidence=cited,
        claimed_value_tier=tier,
        claimed_value_basis=basis,
        claimed_evidence_refs=refs,
        claimed_support=support,
        motivating_signals=motivating,
        run_id=resolved_run,
        created_at=_coerce_str(_first(data, "created_at", default="")) or None,
        raw=data,
    )
