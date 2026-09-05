"""proposal_synthesizer.gate — Deterministic substantiveness gate.

Spec: internal/spec-proposal-synthesizer-2026-05-10.md §4.

The gate is a pure function over a batch of pending candidates. It
emits decisions; the caller (typically ``proposal_synthesizer.run``)
acts on them by moving files and writing logs.

Four rules, applied in order:

  1. **Repetition gate** (§4.1) — drop singletons; require N
     occurrences in window W. Acute candidates skip this rule.
  2. **Magnitude gate** (§4.2) — drop candidates below the
     per-unit floor.
  3. **Aggregation pass** (§4.3) — fold same-fingerprint candidates
     into one (bot_pattern), or same-(generator, variant) across ≥3
     bots into a substrate candidate.
  4. **Concreteness gate** (§4.4) — Investigation-only candidates
     without a named tunable get demoted to watchlist.

The gate does NOT do semantic aggregation across different
fingerprints — that's the synthesizer's job (§5). The gate only
recognizes exact-fingerprint matches.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from schema.candidate_proposal import (
    AggregationKind,
    CandidateProposal,
    new_candidate_id,
)
from proposal_synthesizer.config import (
    DEFAULTS,
    VariantConfig,
    get_variant_config,
    magnitude_floor_for,
)
from proposal_synthesizer.store import repetition_index_path


# ─────────────────────────────────────────────────────────────────────────────
# Decision shape
# ─────────────────────────────────────────────────────────────────────────────


Disposition = Literal["pass", "drop", "watchlist", "aggregated_into"]


@dataclass
class GateDecision:
    """One decision per input candidate (after aggregation, the same
    decision may reference a freshly-minted aggregate candidate)."""

    candidate: CandidateProposal
    disposition: Disposition
    reason: str = ""
    note: str = ""
    # When ``disposition == "aggregated_into"``, this is the id of the
    # aggregate candidate this one was folded into.
    aggregated_into_id: str | None = None


@dataclass
class GateResult:
    """Outcome of a single gate run over a batch."""

    passed: list[CandidateProposal] = field(default_factory=list)
    watchlist: list[CandidateProposal] = field(default_factory=list)
    decisions: list[GateDecision] = field(default_factory=list)
    # Candidates produced by aggregation that did NOT exist in the
    # input batch (aggregate placeholders the caller should persist
    # alongside passed candidates).
    new_aggregates: list[CandidateProposal] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Repetition index (persisted across runs)
# ─────────────────────────────────────────────────────────────────────────────


def load_repetition_index(shared_dir: Path) -> dict[str, list[str]]:
    """Return ``{fingerprint: [timestamp_iso, ...]}`` from disk.

    Returns an empty dict if the index file does not yet exist or is
    unreadable (e.g. partial write — caller can rebuild).
    """
    path = repetition_index_path(shared_dir)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): [str(t) for t in (v or [])] for k, v in raw.items()}


def save_repetition_index(
    shared_dir: Path, index: dict[str, list[str]]
) -> None:
    """Persist the repetition index atomically."""
    path = repetition_index_path(shared_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Same atomic-write pattern as the rest of the store.
    import os
    import tempfile

    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(index, fh, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _prune_window(
    index: dict[str, list[str]], *, window_days: int, now: datetime | None = None
) -> dict[str, list[str]]:
    """Drop timestamps older than the window. Removes empty fingerprints."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=window_days)
    pruned: dict[str, list[str]] = {}
    for fp, stamps in index.items():
        keep = []
        for s in stamps:
            try:
                ts = datetime.fromisoformat(s)
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                keep.append(s)
        if keep:
            pruned[fp] = keep
    return pruned


# ─────────────────────────────────────────────────────────────────────────────
# Rule 4.1 — Repetition gate
# ─────────────────────────────────────────────────────────────────────────────


def _is_acute(candidate: CandidateProposal, vc: VariantConfig) -> bool:
    """Spec §4.1 acute exemption: urgency + magnitude both meet the
    variant's acute thresholds."""
    if vc.acute_urgency is None or vc.acute_magnitude_threshold is None:
        return False
    if candidate.draft_urgency != vc.acute_urgency:
        return False
    if candidate.magnitude is None:
        return False
    return candidate.magnitude.value >= vc.acute_magnitude_threshold


def _passes_repetition(
    candidate: CandidateProposal,
    index: dict[str, list[str]],
    *,
    vc: VariantConfig,
) -> tuple[bool, int]:
    """Return (passes, observed_count).

    ``observed_count`` is the size of the fingerprint's history within
    the window AFTER this candidate is included.
    """
    if _is_acute(candidate, vc):
        return True, 1 + len(index.get(candidate.fingerprint, []))

    min_n = vc.repetition_min_occurrences or DEFAULTS.repetition_min_occurrences
    history = index.get(candidate.fingerprint, [])
    count = 1 + len(history)  # this occurrence + prior occurrences in window
    return count >= min_n, count


# ─────────────────────────────────────────────────────────────────────────────
# Rule 4.2 — Magnitude gate
# ─────────────────────────────────────────────────────────────────────────────


def _passes_magnitude(
    candidate: CandidateProposal, *, vc: VariantConfig
) -> tuple[bool, float, float]:
    """Return (passes, value, floor)."""
    if candidate.magnitude is None:
        # No magnitude declared → use floor 0.0; effectively a pass on
        # this rule. Logged so we can spot generators that should be
        # declaring magnitude but aren't.
        return True, 0.0, 0.0
    floor = magnitude_floor_for(candidate.magnitude.unit, vc.magnitude_floor)
    return candidate.magnitude.value >= floor, candidate.magnitude.value, floor


# ─────────────────────────────────────────────────────────────────────────────
# Rule 4.3 — Aggregation pass
# ─────────────────────────────────────────────────────────────────────────────


def _substrate_fingerprint(generator_id: str, variant: str) -> str:
    """The fingerprint a substrate aggregate uses.

    Distinct from any per-bot fingerprint so substrate aggregation
    doesn't collide with bot_pattern aggregation.
    """
    return f"{generator_id}:{variant}:<substrate>"


def _aggregate_substrate(
    bucket: list[CandidateProposal],
) -> CandidateProposal:
    """Build a substrate-level aggregate from K per-bot candidates
    that share (generator_id, variant)."""
    sample = bucket[0]
    aggregated_signals: list[str] = []
    for c in bucket:
        aggregated_signals.extend(c.motivating_signals)

    # Sum magnitudes when they share a unit; otherwise leave None and
    # let the synthesizer decide what to make of it.
    mag = None
    units = {c.magnitude.unit for c in bucket if c.magnitude is not None}
    if len(units) == 1:
        from schema.candidate_proposal import Magnitude

        unit = units.pop()
        total = sum(c.magnitude.value for c in bucket if c.magnitude is not None)
        mag = Magnitude(unit=unit, value=total)

    bot_names = sorted({c.bot_id for c in bucket})
    headline = (
        f"Address {sample.variant} substrate-wide "
        f"— same condition on {len(bot_names)} bots"
    )[:120]
    problem = (
        f"{len(bot_names)} bots ({', '.join(bot_names)}) all show "
        f"the same condition: {sample.draft_problem}"
    )

    return CandidateProposal(
        id=new_candidate_id(),
        bot_id="<pod>",
        state="pending",
        generator_id=sample.generator_id,
        dimension=sample.dimension,
        variant=sample.variant,
        trigger_observations=list(
            dict.fromkeys(
                obs for c in bucket for obs in c.trigger_observations
            )
        ),
        provenance=sample.provenance,
        motivating_signals=list(dict.fromkeys(aggregated_signals)),
        fingerprint=_substrate_fingerprint(sample.generator_id, sample.variant),
        aggregation="substrate",
        aggregated_from=[c.id for c in bucket],
        magnitude=mag,
        draft_problem=problem,
        draft_headline=headline,
        # The substrate aggregate drops the per-bot draft_action; the
        # synthesizer is responsible for proposing a substrate-level
        # change (e.g. a deploy-time default), which the per-bot
        # actions don't capture.
        draft_action=None,
        draft_claim=None,
        draft_risk_tag=None,
        draft_urgency=sample.draft_urgency,
        draft_approval_audience="pod_operator",
        confidence=min(c.confidence for c in bucket if c.confidence > 0)
        if any(c.confidence > 0 for c in bucket)
        else 0.0,
    )


def _aggregate_bot_pattern(
    bucket: list[CandidateProposal],
) -> CandidateProposal:
    """Build a bot-pattern aggregate from K candidates that share a
    fingerprint (same generator + variant + bot)."""
    # Pick the most recent as the representative — it has the freshest
    # magnitude estimate and the latest draft prose.
    bucket_sorted = sorted(bucket, key=lambda c: c.created_at, reverse=True)
    sample = bucket_sorted[0]

    aggregated_signals: list[str] = []
    for c in bucket:
        aggregated_signals.extend(c.motivating_signals)

    sample.motivating_signals = list(dict.fromkeys(aggregated_signals))
    sample.aggregation = "bot_pattern"
    sample.aggregated_from = [c.id for c in bucket if c.id != sample.id]
    return sample


# ─────────────────────────────────────────────────────────────────────────────
# Rule 4.4 — Concreteness gate
# ─────────────────────────────────────────────────────────────────────────────


# Heuristic match for "names a specific tunable" in draft_action.context
# or draft_problem / draft_headline. We look for any of:
#   - a path under .openclaw/ (e.g. /Users/<bot>/.openclaw/openclaw.json)
#   - a JSON key path in dotted form (e.g. agents.defaults.heartbeat.model)
#   - a backticked identifier (likely a config key or filename)
#   - a cron id reference (e.g. `cron-id-...`)
_TUNABLE_PATTERNS = [
    re.compile(r"\.openclaw/[A-Za-z0-9_./-]+"),
    re.compile(r"\b[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){2,}"),  # dotted.path.key
    re.compile(r"`[A-Za-z0-9_./-]+`"),
    re.compile(r"\bcron[\s_-]?(?:id|name)\b", re.IGNORECASE),
]


def _names_a_tunable(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in _TUNABLE_PATTERNS)


def _passes_concreteness(candidate: CandidateProposal, *, vc: VariantConfig) -> bool:
    """Spec §4.4: Investigation actions without a named tunable get
    demoted. Variants flagged ``concreteness_exempt`` skip this rule.
    """
    if vc.concreteness_exempt:
        return True
    action = candidate.draft_action
    if action is None:
        # No action at all — only allowed for substrate aggregates
        # (which the synthesizer will redraft) or explicitly exempt
        # variants.
        return candidate.aggregation == "substrate"
    kind = getattr(action, "kind", "")
    if kind != "Investigation":
        return True  # concrete actions (TierAdjustment, AgentsAppend, ...)
    # Look in the action's context, and fall back to the candidate's
    # drafted problem / headline.
    context = getattr(action, "context", "") or ""
    if _names_a_tunable(context):
        return True
    if _names_a_tunable(candidate.draft_problem):
        return True
    if _names_a_tunable(candidate.draft_headline):
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Top-level gate
# ─────────────────────────────────────────────────────────────────────────────


def run_gate(
    candidates: list[CandidateProposal],
    *,
    repetition_index: dict[str, list[str]],
    now: datetime | None = None,
) -> GateResult:
    """Apply the four gate rules to a batch of pending candidates.

    Returns a :class:`GateResult` with per-candidate decisions plus
    any new aggregate candidates produced by §4.3. The caller is
    responsible for persisting state changes (the gate is pure).

    The repetition index is consulted but NOT updated; the caller
    should call :func:`update_repetition_index` after processing
    decisions so a single batch with multiple occurrences of the same
    fingerprint counts only once toward the index.
    """
    now = now or datetime.now(timezone.utc)
    result = GateResult()

    # Stage A — rule 4.1 + 4.2 + 4.4 on each candidate independently.
    # Survivors go to stage B for aggregation.
    stage_a_survivors: list[CandidateProposal] = []

    for cand in candidates:
        vc = get_variant_config(cand.generator_id, cand.variant)

        # Rule 4.1 — repetition
        rep_ok, observed = _passes_repetition(cand, repetition_index, vc=vc)
        if not rep_ok:
            min_n = vc.repetition_min_occurrences or DEFAULTS.repetition_min_occurrences
            result.decisions.append(
                GateDecision(
                    candidate=cand,
                    disposition="drop",
                    reason="below_repetition_floor",
                    note=f"observed {observed} of {min_n} required",
                )
            )
            continue

        # Rule 4.2 — magnitude
        mag_ok, value, floor = _passes_magnitude(cand, vc=vc)
        if not mag_ok:
            unit = cand.magnitude.unit if cand.magnitude else "<none>"
            result.decisions.append(
                GateDecision(
                    candidate=cand,
                    disposition="drop",
                    reason="below_magnitude_floor",
                    note=f"{value} {unit} < floor {floor}",
                )
            )
            continue

        # Rule 4.4 — concreteness (run before aggregation so per-bot
        # candidates that lack a named tunable get demoted at the
        # right granularity, not folded into a substrate aggregate
        # they shouldn't escape via).
        if not _passes_concreteness(cand, vc=vc):
            result.watchlist.append(cand)
            result.decisions.append(
                GateDecision(
                    candidate=cand,
                    disposition="watchlist",
                    reason="concreteness_demoted",
                    note="Investigation action without named tunable",
                )
            )
            continue

        stage_a_survivors.append(cand)

    # Stage B — rule 4.3 aggregation.
    #
    # Two passes:
    #   - substrate first: same (generator, variant) across ≥N bots
    #     collapse into one substrate candidate. Removes those
    #     per-bot candidates from the bot-pattern pool.
    #   - bot_pattern second: candidates with identical fingerprints
    #     fold into one.

    # Bucket by (generator_id, variant) for substrate detection.
    by_genvar: dict[tuple[str, str], list[CandidateProposal]] = {}
    for c in stage_a_survivors:
        if c.aggregation == "substrate":
            # Already a substrate aggregate (passed through from a
            # prior run). Pass-through unchanged.
            result.passed.append(c)
            result.decisions.append(
                GateDecision(
                    candidate=c, disposition="pass", reason="passed_gate"
                )
            )
            continue
        by_genvar.setdefault((c.generator_id, c.variant), []).append(c)

    used_in_substrate: set[str] = set()
    for (gid, var), bucket in by_genvar.items():
        distinct_bots = {c.bot_id for c in bucket}
        if len(distinct_bots) >= DEFAULTS.substrate_min_bots:
            agg = _aggregate_substrate(bucket)
            result.new_aggregates.append(agg)
            result.passed.append(agg)
            for c in bucket:
                used_in_substrate.add(c.id)
                result.decisions.append(
                    GateDecision(
                        candidate=c,
                        disposition="aggregated_into",
                        reason="aggregated_substrate",
                        aggregated_into_id=agg.id,
                    )
                )

    # Bucket remaining candidates by fingerprint for bot_pattern
    # folding. A single candidate per fingerprint passes through;
    # K candidates fold into one and the rest are recorded as
    # ``aggregated_into``.
    by_fp: dict[str, list[CandidateProposal]] = {}
    for c in stage_a_survivors:
        if c.id in used_in_substrate:
            continue
        if c.aggregation == "substrate":
            continue  # already passed above
        by_fp.setdefault(c.fingerprint, []).append(c)

    for fp, bucket in by_fp.items():
        if len(bucket) == 1:
            c = bucket[0]
            result.passed.append(c)
            result.decisions.append(
                GateDecision(
                    candidate=c, disposition="pass", reason="passed_gate"
                )
            )
        else:
            agg = _aggregate_bot_pattern(bucket)
            result.passed.append(agg)
            result.decisions.append(
                GateDecision(
                    candidate=agg,
                    disposition="pass",
                    reason="passed_gate_bot_pattern",
                )
            )
            for c in bucket:
                if c.id == agg.id:
                    continue
                result.decisions.append(
                    GateDecision(
                        candidate=c,
                        disposition="aggregated_into",
                        reason="aggregated_bot_pattern",
                        aggregated_into_id=agg.id,
                    )
                )

    return result


def update_repetition_index(
    index: dict[str, list[str]],
    candidates: list[CandidateProposal],
    *,
    window_days: int = DEFAULTS.repetition_window_days,
    now: datetime | None = None,
) -> dict[str, list[str]]:
    """Add candidates to the index and prune to the window.

    Called after :func:`run_gate` so a batch with multiple occurrences
    of the same fingerprint counts only one full pass (not one per
    occurrence) toward future repetition checks.
    """
    now = now or datetime.now(timezone.utc)
    ts = now.isoformat(timespec="seconds")
    updated = {k: list(v) for k, v in index.items()}
    seen_in_batch: set[str] = set()
    for c in candidates:
        if c.fingerprint in seen_in_batch:
            continue
        seen_in_batch.add(c.fingerprint)
        updated.setdefault(c.fingerprint, []).append(ts)
    return _prune_window(updated, window_days=window_days, now=now)
