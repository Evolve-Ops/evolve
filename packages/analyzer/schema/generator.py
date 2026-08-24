"""schema.generator — Charter, Invariant, GeneratorRecord, TrackRecord.

Spec: docs/archive/specs/spec-rsi-layer-1-foundation-2026-04-18.md §4.

A generator is defined by three pieces:

  - Charter: immutable identity + invariants (shipped with code as YAML)
  - GeneratorRecord: evolvable config + runtime state + track record (in shared
    state as JSON, per bot or pod-wide depending on scope)
  - Handler code: the Python module that implements ``observe()`` and optional
    ``evaluate()`` (for guardians). Not defined here — lives next to the charter.

The registry is responsible for pairing Charter and GeneratorRecord at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


CHARTER_SCHEMA_VERSION = 1


# ─────────────────────────────────────────────────────────────────────────────
# Enums / Literals
# ─────────────────────────────────────────────────────────────────────────────

GeneratorType = Literal["optimizer", "guardian", "meta_guardian"]

Cadence = Literal["on_demand", "hourly", "daily", "weekly"]

GeneratorStatus = Literal["active", "paused", "quarantined"]

# Four-bucket capability framing — see docs/product-vision.md and
# memory/project_evolve_four_buckets. Every generator belongs in exactly one
# bucket; the value is part of the charter (immutable identity) and surfaces
# in the admin UI as the primary grouping on the Generators tab.
Bucket = Literal["operate", "extend", "improve", "access"]

# Operator-facing surface routing — which section of the admin UI this
# generator's proposals land in. Distinct from ``bucket`` (which is the
# capability framing on the Generators tab); ``surface`` answers "where
# does the operator *see* these findings".
#
# Spec: internal/spec-recommendations-rework-2026-06-02.md §"UI shape".
#
#   firing      — actively broken state right now (sensor-blocking
#                 misconfigs, CRITICAL audit findings). Loud.
#   drift       — slow drift / install hygiene. The operator should look,
#                 but no fire is burning.
#   cleanup     — low-stakes hygiene. Collapsed by default on the page;
#                 the operator can bulk-accept or bulk-ignore.
#   improvement — sparse, opinionated product suggestions for apps the
#                 operator is using (app-usage advisor and similar).
#
# Phase 1 (this rework) populates only the worst-offender generators
# per the spec routing table; the rest stay None and route via the
# fallback heuristic in the UI until Phase 2 closes the sweep.
Surface = Literal["firing", "drift", "cleanup", "improvement"]

InvariantCheckKind = Literal[
    "action_kind_allowed",
    "touches_forbidden",
    "claim_required",
    "human_approval_for",
    "never_self_expands",
    "claim_metric_known",
    "custom",
]

BudgetPolicy = Literal["competitive", "duty", "meta"]


def _clean_subscribes_to(raw: object) -> list[str]:
    """Normalize the ``subscribes_to`` charter field into a list of Signal type strings.

    Accepts an absent/None value (returns ``[]``) or a list of strings. A
    malformed value (e.g. a single string instead of a list) is coerced
    to ``[]`` rather than raised so a typo in one charter does not block
    the whole registry from loading.
    """
    if not raw:
        return []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Invariant
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Invariant:
    """A machine-checkable assertion about a generator's proposals or config.

    Invariants are declared in a charter (YAML, ships with code). They are
    checked in two places:

      1. On every proposal the generator emits, before the arbiter accepts it.
      2. On every self-update the generator proposes to its own config.

    A generator that emits a proposal violating its own invariant is a bug
    and gets quarantined by the registry (status = ``"quarantined"``).
    """

    id: str
    description: str
    check_kind: InvariantCheckKind
    params: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Invariant.id must be non-empty")
        if not self.description:
            raise ValueError("Invariant.description must be non-empty")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "check_kind": self.check_kind,
            "params": dict(self.params),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Invariant":
        return cls(
            id=data["id"],
            description=data["description"],
            check_kind=data["check_kind"],
            params=dict(data.get("params") or {}),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Charter — immutable identity, loaded from YAML
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Charter:
    """A generator's immutable identity.

    Charters are declared in ``packages/analyzer/generators/<id>/charter.yaml``
    and loaded by the registry at startup. They are **immutable at runtime** —
    changing a charter ships as code, reviewed in PR. The registry refuses to
    load a generator whose charter fingerprint differs from the fingerprint
    recorded in the generator's GeneratorRecord.
    """

    id: str
    type: GeneratorType
    dimension: str
    purpose: str
    cadence: Cadence
    # Four-bucket capability framing. None for charters that predate the field;
    # new charters must declare one. The admin UI groups generators by bucket.
    bucket: Bucket | None = None
    # Operator-facing surface routing — see Surface docstring above. None
    # for charters that haven't been classified yet (Phase 2 sweep will
    # close them out). Distinct from ``bucket``.
    surface: Surface | None = None
    # Altitude / value-ambition tier the generator's proposals default to —
    # L0 hygiene/substrate · L1 optimization · L2 capability · L3 strategic.
    # Orthogonal to ``surface`` (where the operator sees it) and ``urgency``
    # (how loudly it asks for attention). Default 0 (L0): every existing
    # generator stays valid without a change; capability generators opt up
    # (e.g. app_suggester → 2). Resolved onto each proposal view at list time
    # (same pattern as ``surface``); a non-zero per-proposal ``altitude`` on
    # the Proposal wins. Spec: internal/spec-fit-reviewer-2026-06-12.md §5.
    altitude: int = 0
    invariants: list[Invariant] = field(default_factory=list)
    schema_version: int = CHARTER_SCHEMA_VERSION
    # Sensor-style generators that re-fire each cycle whenever their issue is
    # present can opt in: when set true, the generator_runner will mark any
    # pending proposal of theirs as ``resolved_externally`` if the current
    # observation cycle did NOT re-emit a matching fingerprint. Insight-style
    # generators (one-shot suggestions that are not expected to keep firing)
    # must leave this false.
    resolves_when_silent: bool = False
    # Event-driven dispatch opt-in (signal-subscriber substrate). When set,
    # the ``signal_subscriber`` daemon watches ``{shared_dir}/signals/firing/``
    # and invokes this generator's observe() immediately whenever a Signal of
    # one of the listed ``type`` values lands. The daily cadence still
    # applies as a safety net (backfill, drift) — subscription is a *latency*
    # reduction, not a replacement. Empty / unset = no event-driven dispatch.
    # Spec: internal/spec-signal-subscriber-2026-05-31.md.
    subscribes_to: list[str] = field(default_factory=list)
    # Background-writer opt-out. Most generators emit Proposals; a few run
    # per-session and write directly to their own datastore (e.g.
    # user_profile_inferrer writes profile facts to disk in each bot).
    # The admin UI uses this flag to suppress emit / verify / track-record
    # columns that would otherwise display zeros and make a working coach
    # look broken. Default True — emitting is the norm.
    emits_proposals: bool = True

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Charter.id must be non-empty")
        if not self.purpose:
            raise ValueError("Charter.purpose must be non-empty")
        if not self.dimension:
            raise ValueError("Charter.dimension must be non-empty")
        # Enforce invariant id uniqueness within a charter
        ids = [inv.id for inv in self.invariants]
        if len(ids) != len(set(ids)):
            raise ValueError(
                f"Charter '{self.id}' has duplicate invariant ids: {ids}"
            )

    def invariant_by_id(self, invariant_id: str) -> Invariant | None:
        for inv in self.invariants:
            if inv.id == invariant_id:
                return inv
        return None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "schema_version": self.schema_version,
            "type": self.type,
            "dimension": self.dimension,
            "purpose": self.purpose,
            "cadence": self.cadence,
            "bucket": self.bucket,
            "surface": self.surface,
            "altitude": self.altitude,
            "invariants": [inv.to_dict() for inv in self.invariants],
            "resolves_when_silent": self.resolves_when_silent,
            "subscribes_to": list(self.subscribes_to),
            "emits_proposals": self.emits_proposals,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Charter":
        return cls(
            id=data["id"],
            schema_version=int(data.get("schema_version", CHARTER_SCHEMA_VERSION)),
            type=data["type"],
            dimension=data["dimension"],
            purpose=data["purpose"],
            cadence=data["cadence"],
            bucket=data.get("bucket"),
            surface=data.get("surface"),
            altitude=int(data.get("altitude") or 0),
            invariants=[
                Invariant.from_dict(inv) for inv in (data.get("invariants") or [])
            ],
            resolves_when_silent=bool(data.get("resolves_when_silent", False)),
            subscribes_to=_clean_subscribes_to(data.get("subscribes_to")),
            emits_proposals=bool(data.get("emits_proposals", True)),
        )


# ─────────────────────────────────────────────────────────────────────────────
# TrackRecord — per-generator lifetime stats
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class DimensionStats:
    """Per-dimension breakdown; populated from L5 onward."""

    proposals_emitted: int = 0
    proposals_succeeded: int = 0
    user_act_rate: float = 0.0
    user_reject_rate: float = 0.0

    def to_dict(self) -> dict:
        return {
            "proposals_emitted": self.proposals_emitted,
            "proposals_succeeded": self.proposals_succeeded,
            "user_act_rate": self.user_act_rate,
            "user_reject_rate": self.user_reject_rate,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DimensionStats":
        return cls(
            proposals_emitted=int(data.get("proposals_emitted", 0)),
            proposals_succeeded=int(data.get("proposals_succeeded", 0)),
            user_act_rate=float(data.get("user_act_rate", 0.0)),
            user_reject_rate=float(data.get("user_reject_rate", 0.0)),
        )


@dataclass
class TrackRecord:
    """Per-generator lifetime stats.

    L1 keeps this flat (lifetime counters). L4 adds lifetime_cost_usd and
    per_verification_outcomes. L5 adds per_dimension_breakdown and
    authority_score. Fields later than L1 default to zero/empty and are
    populated by their respective layers.
    """

    proposals_emitted: int = 0
    proposals_applied: int = 0
    proposals_verified_success: int = 0
    proposals_verified_failed: int = 0
    proposals_rejected_human: int = 0
    proposals_vetoed_guardian: int = 0
    last_verification_at: str | None = None
    last_update_at: str | None = None

    # Iteration split (subsets of proposals_verified_success): how many
    # successes happened on the first try vs. after one or more operator-
    # driven Refine cycles. ``compute_authority`` discounts after-iteration
    # successes (default 0.5×) so generators that need fewer rounds score
    # higher. ``first_shot + after_iteration`` always equals
    # ``proposals_verified_success``.
    proposals_succeeded_first_shot: int = 0
    proposals_succeeded_after_iteration: int = 0

    # L4+ fields
    lifetime_cost_usd: float = 0.0

    # L5+ fields
    per_dimension_breakdown: dict = field(default_factory=dict)
    authority_score: float = 1.0

    def to_dict(self) -> dict:
        return {
            "proposals_emitted": self.proposals_emitted,
            "proposals_applied": self.proposals_applied,
            "proposals_verified_success": self.proposals_verified_success,
            "proposals_verified_failed": self.proposals_verified_failed,
            "proposals_succeeded_first_shot": self.proposals_succeeded_first_shot,
            "proposals_succeeded_after_iteration": self.proposals_succeeded_after_iteration,
            "proposals_rejected_human": self.proposals_rejected_human,
            "proposals_vetoed_guardian": self.proposals_vetoed_guardian,
            "last_verification_at": self.last_verification_at,
            "last_update_at": self.last_update_at,
            "lifetime_cost_usd": self.lifetime_cost_usd,
            "per_dimension_breakdown": {
                k: (v.to_dict() if isinstance(v, DimensionStats) else dict(v))
                for k, v in self.per_dimension_breakdown.items()
            },
            "authority_score": self.authority_score,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TrackRecord":
        breakdown_raw = data.get("per_dimension_breakdown") or {}
        breakdown: dict = {}
        for dim, stats in breakdown_raw.items():
            breakdown[dim] = DimensionStats.from_dict(stats)
        return cls(
            proposals_emitted=int(data.get("proposals_emitted", 0)),
            proposals_applied=int(data.get("proposals_applied", 0)),
            proposals_verified_success=int(
                data.get("proposals_verified_success", 0)
            ),
            proposals_verified_failed=int(data.get("proposals_verified_failed", 0)),
            proposals_succeeded_first_shot=int(
                data.get("proposals_succeeded_first_shot", 0)
            ),
            proposals_succeeded_after_iteration=int(
                data.get("proposals_succeeded_after_iteration", 0)
            ),
            proposals_rejected_human=int(data.get("proposals_rejected_human", 0)),
            proposals_vetoed_guardian=int(data.get("proposals_vetoed_guardian", 0)),
            last_verification_at=data.get("last_verification_at"),
            last_update_at=data.get("last_update_at"),
            lifetime_cost_usd=float(data.get("lifetime_cost_usd", 0.0)),
            per_dimension_breakdown=breakdown,
            authority_score=float(data.get("authority_score", 1.0)),
        )


# ─────────────────────────────────────────────────────────────────────────────
# GeneratorRecord — persistent, evolvable state
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class GeneratorRecord:
    """A generator's persistent state.

    Lives at ``{shared_dir}/generators/{id}.json`` (atomic writes). Loaded by
    the registry; mutated only through registry-provided APIs. Charter
    fingerprint is checked at load time — if the deployed charter YAML
    differs, the registry refuses to load and reports a drift.
    """

    id: str
    charter_fingerprint: str  # SHA-256 of charter.yaml at deployment
    config: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)
    track_record: TrackRecord = field(default_factory=TrackRecord)
    status: GeneratorStatus = "active"
    budget_policy: BudgetPolicy = "competitive"
    quarantine_reason: str | None = None

    # L6+ fields
    competitive_group: str | None = None
    competitive_weight: float = 1.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "charter_fingerprint": self.charter_fingerprint,
            "config": dict(self.config),
            "state": dict(self.state),
            "track_record": self.track_record.to_dict(),
            "status": self.status,
            "budget_policy": self.budget_policy,
            "quarantine_reason": self.quarantine_reason,
            "competitive_group": self.competitive_group,
            "competitive_weight": self.competitive_weight,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GeneratorRecord":
        return cls(
            id=data["id"],
            charter_fingerprint=data["charter_fingerprint"],
            config=dict(data.get("config") or {}),
            state=dict(data.get("state") or {}),
            track_record=TrackRecord.from_dict(data.get("track_record") or {}),
            status=data.get("status", "active"),
            budget_policy=data.get("budget_policy", "competitive"),
            quarantine_reason=data.get("quarantine_reason"),
            competitive_group=data.get("competitive_group"),
            competitive_weight=float(data.get("competitive_weight", 1.0)),
        )
