"""schema — RSI / Better Engine data model (L1).

All dataclasses for Proposal, Action variants, Claim, RevertPlan, RiskTag,
Charter, Invariant, GeneratorRecord, TrackRecord, and related types.

See docs/archive/specs/spec-rsi-layer-1-foundation-2026-04-18.md for the authoritative spec.
"""

from schema.proposal import (
    Action,
    AgentsAppend,
    ApprovalAudience,
    Claim,
    ConfigPatch,
    ConflictAnnotation,
    Direction,
    DeprecateApp,
    Fallback,
    GuardianAnnotation,
    InstallApp,
    Investigation,
    ManifestUpdate,
    MemoryCurate,
    PauseGenerator,
    Proposal,
    PROPOSAL_SCHEMA_VERSION,
    Reversibility,
    RevertPlan,
    RiskTag,
    SoulEdit,
    Status,
    StatusTransition,
    TierAdjustment,
    ThrottleGenerator,
    Urgency,
    VetoAnnotation,
    WorkflowInstruction,
)
from schema.generator import (
    Charter,
    CHARTER_SCHEMA_VERSION,
    Cadence,
    DimensionStats,
    GeneratorRecord,
    GeneratorStatus,
    GeneratorType,
    Invariant,
    InvariantCheckKind,
    TrackRecord,
)
from schema.provenance import (
    Provenance,
)
from schema.observation import (
    ClusterCell,
    ClusterSpec,
    ClusterView,
    MOOD_VOCABULARY,
    ObservationTuple,
    VERB_VOCABULARY,
    new_tuple_id,
)
from schema.veto import (
    MergeDecision,
    MergeJudgment,
    VetoResult,
    VetoSeverity,
    VetoVerdict,
)
from schema.watchdog import (
    WatchdogEvent,
    WatchdogEventType,
    WatchdogSeverity,
    new_watchdog_event_id,
)

__all__ = [
    # proposal
    "Action",
    "AgentsAppend",
    "ApprovalAudience",
    "Claim",
    "ConfigPatch",
    "ConflictAnnotation",
    "Direction",
    "DeprecateApp",
    "Fallback",
    "GuardianAnnotation",
    "InstallApp",
    "Investigation",
    "ManifestUpdate",
    "MemoryCurate",
    "PauseGenerator",
    "Proposal",
    "PROPOSAL_SCHEMA_VERSION",
    "Reversibility",
    "RevertPlan",
    "RiskTag",
    "SoulEdit",
    "Status",
    "StatusTransition",
    "TierAdjustment",
    "ThrottleGenerator",
    "Urgency",
    "VetoAnnotation",
    "WorkflowInstruction",
    # generator
    "Charter",
    "CHARTER_SCHEMA_VERSION",
    "Cadence",
    "DimensionStats",
    "GeneratorRecord",
    "GeneratorStatus",
    "GeneratorType",
    "Invariant",
    "InvariantCheckKind",
    "TrackRecord",
    # provenance
    "Provenance",
    # observation (L3)
    "ClusterCell",
    "ClusterSpec",
    "ClusterView",
    "MOOD_VOCABULARY",
    "ObservationTuple",
    "VERB_VOCABULARY",
    "new_tuple_id",
    # veto / merge (L3)
    "MergeDecision",
    "MergeJudgment",
    "VetoResult",
    "VetoSeverity",
    "VetoVerdict",
    # watchdog (L6)
    "WatchdogEvent",
    "WatchdogEventType",
    "WatchdogSeverity",
    "new_watchdog_event_id",
]
