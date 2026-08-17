"""arbiter.ingest — Validate and accept proposals into the arbiter.

Spec: docs/archive/specs/spec-rsi-layer-1-foundation-2026-04-18.md §5.1 and §5.3.

Ingest pipeline:
  1. Schema validation (reject on malformed)
  2. Invariant check against the emitting generator's charter
  3. Dedup hook — on collision, merge into the existing open proposal
     (refresh trigger_observations, re-surface from snoozed, append a
     refresh entry to history) and signal the caller to skip writing
     the new one. The ``run_dedup_hook`` primitive ships in L1 as a
     fingerprint computer; L2 (this module) acts on the collisions.
  4. Routing (autonomous vs human; surface audience)
  5. Transition to ``pending`` and persist

This module owns steps 1–3 and returns a structured result. The caller
(registry / daemon) handles the ``pending`` transition and persistence.
Step 4 (routing) lives in ``arbiter.routing`` and is typically invoked
by the caller after ingest returns.

L1 does NOT ship signature verification here because ``evolve_config.verify_proposal_sig``
already owns that. The ingest pipeline calls it if a signature is present.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from arbiter.dedup import DedupResult, run_dedup_hook
from arbiter.state_machine import (
    IllegalTransitionError,
    is_legal_transition,
    transition,
)
from schema.generator import Charter, GeneratorRecord, Invariant
from schema.proposal import IRREVERSIBILITY_SURFACES, Proposal, StatusTransition


class IngestError(Exception):
    """Base class for ingest-time errors."""


class SchemaError(IngestError):
    """Raised when a proposal is structurally invalid."""


class InvariantViolation(IngestError):
    """Raised when a proposal violates its own generator's charter invariant.

    Attached fields:
      - ``invariant``: the Invariant that was violated
      - ``proposal_id``: the offending proposal's id
      - ``generator_id``: the emitting generator's id
    """

    def __init__(
        self,
        invariant: Invariant,
        *,
        proposal_id: str,
        generator_id: str,
        detail: str = "",
    ) -> None:
        self.invariant = invariant
        self.proposal_id = proposal_id
        self.generator_id = generator_id
        self.detail = detail
        msg = (
            f"Invariant violation: generator={generator_id!r} invariant={invariant.id!r}"
            f" on proposal={proposal_id!r}"
        )
        if detail:
            msg += f" ({detail})"
        super().__init__(msg)


@dataclass
class IngestResult:
    """Outcome of ``ingest()`` for a single proposal.

    When ``merged_into_id`` is set, the incoming proposal collided with an
    already-open proposal of the same fingerprint. The caller MUST NOT write
    the incoming proposal; it should instead persist ``refreshed_proposal``
    (which has been mutated in place — new triggers merged in, history
    appended, and possibly re-surfaced from snoozed → pending).
    """

    proposal: Proposal
    accepted: bool
    dedup: DedupResult | None = None
    merged_into_id: str | None = None
    refreshed_proposal: Proposal | None = None
    refreshed_from_status: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def should_write_new(self) -> bool:
        """True iff the caller should write the *incoming* proposal as a new
        file. False when the proposal was merged into an existing one (the
        caller persists ``refreshed_proposal`` instead)."""
        return self.accepted and self.merged_into_id is None


# ─────────────────────────────────────────────────────────────────────────────
# Schema validation
# ─────────────────────────────────────────────────────────────────────────────


def _validate_schema(proposal: Proposal) -> None:
    """Minimal structural validation at ingest time.

    Most validation is already enforced by dataclass construction. What
    this covers:

      - Schema version is at v2 or higher.

    Note: ``revert_on_failure`` is NOT required at ingest. The applier
    captures the snapshot and populates ``revert_on_failure`` at apply
    time (see ``arbiter.apply``). The verify daemon checks for its
    presence at verification time (see ``verify.dispatch._attempt_revert``).
    Requiring it here would force generators to capture snapshots they
    have no business capturing.
    """
    if proposal.schema_version < 2:
        raise SchemaError(
            f"proposal {proposal.id}: schema_version={proposal.schema_version} "
            "below minimum (2)"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Invariant checking
# ─────────────────────────────────────────────────────────────────────────────


def _check_invariant(
    proposal: Proposal, invariant: Invariant, known_metrics: Iterable[str] | None
) -> tuple[bool, str]:
    """Check one invariant against a proposal.

    Returns ``(passed, detail)``. If ``passed`` is False, ``detail`` should
    explain the violation in human-readable terms.
    """
    kind = invariant.check_kind
    params = invariant.params

    if kind == "action_kind_allowed":
        allowlist = set(params.get("allowlist") or [])
        actual = getattr(proposal.action, "kind", type(proposal.action).__name__)
        if actual not in allowlist:
            return False, f"action.kind={actual!r} not in allowlist {sorted(allowlist)!r}"
        return True, ""

    if kind == "touches_forbidden":
        forbidden = set(params.get("forbidden") or [])
        actual = set(proposal.risk_tag.touches)
        overlap = actual & forbidden
        if overlap:
            return (
                False,
                f"risk_tag.touches contains forbidden surfaces {sorted(overlap)!r}",
            )
        return True, ""

    if kind == "claim_required":
        if proposal.claim is None:
            return False, "claim is required but missing"
        return True, ""

    if kind == "human_approval_for":
        # Proposals matching a condition must route to human approval.
        # Spec leaves the condition free-form in ``params``; L1 supports
        # the two most useful shapes:
        #   params={"action_kinds": ["InstallApp", ...]}  → those kinds must not be autonomous
        #   params={"touches": ["auth", ...]}             → proposals touching these must not be autonomous
        action_kinds = set(params.get("action_kinds") or [])
        touches = set(params.get("touches") or [])
        kind_actual = getattr(
            proposal.action, "kind", type(proposal.action).__name__
        )
        touches_actual = set(proposal.risk_tag.touches)
        triggered = False
        if action_kinds and kind_actual in action_kinds:
            triggered = True
        if touches and touches & touches_actual:
            triggered = True
        if triggered and proposal.approval_audience == "none":
            return (
                False,
                "proposal matches human_approval_for condition but audience is 'none'",
            )
        return True, ""

    if kind == "never_self_expands":
        # Self-update safety. For proposals against the generator's own config
        # (represented as ConfigPatch on a generator-config path or as
        # AdjustDimensionWeight in L5+), forbid modifications to listed fields.
        forbidden_fields = set(params.get("forbidden_fields") or [])
        # L1 implementation: inspect action.fields on ManifestUpdate or action
        # attributes. Full enforcement depends on action variant semantics
        # landing later layers; L1 ships the gate.
        # For L1 we simply pass (nothing to catch) — enforcement activates
        # when the self-update pathway exists (L5+).
        if not forbidden_fields:
            return True, ""
        # Best-effort: if action carries an explicit ``target_path`` or
        # ``field`` naming a forbidden field, flag it.
        for forbidden in forbidden_fields:
            for attr_name in ("target_path", "field", "dimension"):
                if hasattr(proposal.action, attr_name):
                    if getattr(proposal.action, attr_name) == forbidden:
                        return False, f"action targets forbidden field {forbidden!r}"
        return True, ""

    if kind == "claim_metric_known":
        # Added in L2 — if known_metrics is provided and claim is present,
        # assert the metric is registered.
        if proposal.claim is None or known_metrics is None:
            return True, ""
        if proposal.claim.metric not in set(known_metrics):
            return (
                False,
                f"claim.metric={proposal.claim.metric!r} not in metric registry",
            )
        return True, ""

    if kind == "custom":
        # L1 cannot evaluate arbitrary custom predicates. Generators ship
        # their own module-supplied checker; if that hook is not provided at
        # ingest time, the custom invariant is logged as "uncheckable" and
        # passes. L2+ may tighten.
        return True, ""

    # Unknown check_kind — log and pass, to be forgiving of forward-compat
    # drift. Tests assert this is never the case for charters we actually
    # ship.
    return True, ""


def _check_invariants(
    proposal: Proposal,
    charter: Charter,
    known_metrics: Iterable[str] | None,
) -> None:
    """Check every invariant in the charter against the proposal.

    Raises ``InvariantViolation`` on the first failure.
    """
    for invariant in charter.invariants:
        passed, detail = _check_invariant(proposal, invariant, known_metrics)
        if not passed:
            raise InvariantViolation(
                invariant,
                proposal_id=proposal.id,
                generator_id=charter.id,
                detail=detail,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Ingest entry point
# ─────────────────────────────────────────────────────────────────────────────


def ingest(
    proposal: Proposal,
    *,
    charter: Charter | None = None,
    open_proposals: Iterable[Proposal] | None = None,
    known_metrics: Iterable[str] | None = None,
    actor: str = "arbiter",
) -> IngestResult:
    """Run the L1 ingest pipeline on a proposal.

    Steps:
      1. Schema validation (raises SchemaError on fail).
      2. Invariant check against the generator's charter (raises InvariantViolation).
         If no charter is supplied (e.g., legacy proposals), invariant check
         is skipped — caller is expected to handle that case explicitly.
      3. Dedup hook — computes fingerprint; L1 doesn't act on collisions.
      4. Transition to ``pending`` if the proposal is still in ``draft``.

    Returns ``IngestResult`` with ``accepted=True`` on success. On failure
    the relevant exception is raised (caller catches and handles).

    NOTE: Ingest does NOT call routing. The caller (registry or daemon) calls
    ``arbiter.routing.route()`` separately, because routing needs a
    ``BotRoutingConfig`` that ingest has no business knowing about.
    """
    notes: list[str] = []

    # Materialize the open-proposals iterable once; we read it twice (dedup
    # hook + collision-merge lookup) and a generator would be exhausted.
    open_list: list[Proposal] | None = (
        list(open_proposals) if open_proposals is not None else None
    )

    # Step 1: schema
    _validate_schema(proposal)
    notes.append("schema ok")

    # Step 2: invariants (if we have a charter)
    if charter is not None:
        _check_invariants(proposal, charter, known_metrics)
        notes.append(f"invariants ok ({len(charter.invariants)} checked)")
    else:
        notes.append("no charter supplied — invariant check skipped")

    # Step 3: dedup
    dedup_result = run_dedup_hook(proposal, open_proposals=open_list)
    if dedup_result.collisions and open_list is not None:
        existing = _resolve_collision(dedup_result.collisions, open_list)
        if existing is not None:
            refreshed_from_status = existing.status
            _refresh_existing(existing, incoming=proposal, actor=actor)
            notes.append(
                f"merged into existing proposal {existing.id} "
                f"(was {refreshed_from_status})"
            )
            return IngestResult(
                proposal=proposal,
                accepted=True,
                dedup=dedup_result,
                merged_into_id=existing.id,
                refreshed_proposal=existing,
                refreshed_from_status=refreshed_from_status,
                notes=notes,
            )
        # Fingerprint collided but the matching proposal isn't in the open
        # list any more (e.g. raced with a status change). Fall through and
        # treat as unique — the next cycle will collapse it.
        notes.append(
            f"fingerprint collisions with {len(dedup_result.collisions)} id(s) "
            "but no live match in open_proposals; treating as new"
        )
    elif dedup_result.collisions:
        notes.append(
            f"fingerprint collisions with {len(dedup_result.collisions)} open proposal(s); "
            "no open_proposals supplied so merge skipped"
        )
    else:
        notes.append("fingerprint unique among open proposals")

    # Step 4: draft → pending
    if proposal.status == "draft":
        if is_legal_transition("draft", "pending"):
            try:
                transition(proposal, "pending", actor=actor, reason="ingest accepted")
                notes.append("transitioned draft → pending")
            except IllegalTransitionError as e:
                # Shouldn't happen (the check above passed), but surface.
                raise SchemaError(
                    f"proposal {proposal.id}: unexpected illegal transition: {e}"
                ) from e

    return IngestResult(
        proposal=proposal,
        accepted=True,
        dedup=dedup_result,
        notes=notes,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Dedup-merge helpers
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_collision(
    collision_ids: list[str], open_proposals: list[Proposal]
) -> Proposal | None:
    """Return the open proposal to merge into.

    When multiple open proposals share a fingerprint (rare; usually the result
    of a prior bug or manual import), pick the oldest by ``created_at`` so
    that re-detections accumulate onto the original record rather than
    fragmenting across copies.
    """
    by_id = {p.id: p for p in open_proposals}
    matches = [by_id[cid] for cid in collision_ids if cid in by_id]
    if not matches:
        return None
    matches.sort(key=lambda p: p.created_at)
    return matches[0]


def _merge_triggers(existing: list[str], incoming: list[str]) -> list[str]:
    """Union, preserving existing order, appending only triggers not already
    present. Keeps the audit trail readable rather than re-sorting."""
    seen = set(existing)
    merged = list(existing)
    for t in incoming:
        if t and t not in seen:
            merged.append(t)
            seen.add(t)
    return merged


def _refresh_existing(existing: Proposal, *, incoming: Proposal, actor: str) -> None:
    """Mutate ``existing`` to record a re-detection from ``incoming``.

    - Merge incoming.trigger_observations into existing.trigger_observations.
    - Overwrite the proposal's user-facing surface (problem, action,
      provenance, claim, admin_surface_summary, and the Phase-A fields
      summary/explanation/action_label/manual_path/conversational_pitch,
      plus motivating_signals when the incoming carries any) with the
      incoming values so the operator sees the *current* framing — current
      dollar amounts, latest recommended config value, etc. The dedup
      fingerprint is the only thing that has to stay constant for collapse
      to work; the framing should freshen on every re-detection. Without
      this the queue shows "admin_bot daily spend $4.23" days after the
      actual figure has moved on.
    - If existing is snoozed, re-surface to pending (a real state transition
      using the state machine) — the issue is firing again.
    - Otherwise append a same-status StatusTransition to history with a
      ``dedup-refresh`` reason; this gives the UI a "last re-detected at"
      timestamp without requiring a schema change.
    """
    existing.trigger_observations = _merge_triggers(
        existing.trigger_observations, incoming.trigger_observations
    )
    existing.problem = incoming.problem
    existing.action = incoming.action
    existing.provenance = incoming.provenance
    existing.claim = incoming.claim
    existing.admin_surface_summary = incoming.admin_surface_summary
    # Phase-A operator-facing prose must freshen alongside the action:
    # for a permission-widening proposal (e.g. autonomy_promoter) the
    # explanation embeds the proposed limits, and a stale explanation next
    # to a freshened action.rules is a consent mismatch at approval time.
    existing.summary = incoming.summary
    existing.explanation = incoming.explanation
    existing.action_label = incoming.action_label
    existing.manual_path = incoming.manual_path
    existing.conversational_pitch = incoming.conversational_pitch
    if incoming.motivating_signals:
        # Overwrite (not union): the incoming detection's Signals are the
        # live motivation, and auto_resolve requires EVERY listed id to be
        # inactive before archiving. Never wipe with an empty list — empty
        # means "cannot reason about freshness" to the auto-resolve sweep,
        # which would strand the proposal out of auto-resolution.
        existing.motivating_signals = list(incoming.motivating_signals)
    refresh_reason = f"dedup-refresh from {incoming.id}"
    if existing.status == "snoozed":
        try:
            transition(existing, "pending", actor=actor, reason=refresh_reason)
            return
        except IllegalTransitionError:
            # Shouldn't happen given the state graph, but if it ever does we
            # fall through to the same-status refresh entry rather than
            # losing the re-detection signal.
            pass
    existing.history.append(
        StatusTransition(
            from_status=existing.status,
            to_status=existing.status,
            at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            actor=actor,
            reason=refresh_reason,
        )
    )
