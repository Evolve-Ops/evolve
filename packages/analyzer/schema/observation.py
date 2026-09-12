"""schema.observation — ObservationTuple and related types (L3).

Spec: docs/archive/specs/spec-rsi-layer-3-cost-security-tuples-2026-04-18.md §3.1.

An ObservationTuple is one unit of "what happened" in a session segment.
Its shape is (noun × verb × mood × engagement), with the noun taxonomy
seeded from installed app manifests and a fixed verb + mood vocabulary
shipped in code.

The fixed verb and mood sets are enforced at extraction time. Nouns are
open — any string that normalizes cleanly is accepted — but extractors
are strongly encouraged to pick from the current manifest-seeded list
when applicable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4


# ─────────────────────────────────────────────────────────────────────────────
# Vocabularies
# ─────────────────────────────────────────────────────────────────────────────

# Fixed verb vocabulary — ~20 cognitive-operation terms. Changes require a
# code change + PR review (not runtime evolution).
VERB_VOCABULARY: frozenset[str] = frozenset(
    {
        "drafting",
        "planning",
        "troubleshooting",
        "tracking",
        "researching",
        "summarizing",
        "reviewing",
        "deciding",
        "learning",
        "explaining",
        "comparing",
        "transforming",
        "scheduling",
        "recording",
        "reminding",
        "searching",
        "coordinating",
        "celebrating",
        "venting",
        "exploring",
    }
)

# Fixed mood vocabulary. ``None`` is also acceptable — extractors may leave
# mood empty when no strong affect signal is present.
MOOD_VOCABULARY: frozenset[str] = frozenset(
    {
        "neutral",
        "urgent",
        "enthusiastic",
        "frustrated",
        "reflective",
        "uncertain",
    }
)


# ─────────────────────────────────────────────────────────────────────────────
# ObservationTuple
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ObservationTuple:
    """One (noun × verb × mood × engagement) observation from a session segment.

    Spec §3.1. Produced by the extraction LLM call and persisted to
    ``{shared_dir}/observations/{bot_id}/{YYYY-MM-DD}.jsonl``.
    """

    id: str
    bot_id: str
    session_id: str
    segment_id: str
    noun: str
    verb: str
    mood: str | None
    engagement: int  # turn pairs in this segment
    timestamp_start: str  # ISO8601 UTC
    timestamp_end: str  # ISO8601 UTC
    source_hash: str  # hash of the segment's turn text (for dedup / link-back)
    extraction_confidence: float = 0.5

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("ObservationTuple.id must be non-empty")
        if not self.bot_id or not self.session_id or not self.segment_id:
            raise ValueError(
                "ObservationTuple: bot_id, session_id, segment_id all required"
            )
        # Noun is open but must be non-empty and lowercase-normalizable
        if not self.noun:
            raise ValueError("ObservationTuple.noun must be non-empty")
        self.noun = self.noun.strip().lower()
        if self.verb not in VERB_VOCABULARY:
            raise ValueError(
                f"ObservationTuple.verb={self.verb!r} not in fixed verb vocabulary"
            )
        if self.mood is not None and self.mood not in MOOD_VOCABULARY:
            raise ValueError(
                f"ObservationTuple.mood={self.mood!r} not in fixed mood vocabulary"
            )
        if self.engagement < 0:
            raise ValueError("ObservationTuple.engagement must be >= 0")
        if not 0.0 <= self.extraction_confidence <= 1.0:
            raise ValueError(
                f"ObservationTuple.extraction_confidence out of range: "
                f"{self.extraction_confidence}"
            )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "bot_id": self.bot_id,
            "session_id": self.session_id,
            "segment_id": self.segment_id,
            "noun": self.noun,
            "verb": self.verb,
            "mood": self.mood,
            "engagement": self.engagement,
            "timestamp_start": self.timestamp_start,
            "timestamp_end": self.timestamp_end,
            "source_hash": self.source_hash,
            "extraction_confidence": self.extraction_confidence,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ObservationTuple":
        return cls(
            id=data["id"],
            bot_id=data["bot_id"],
            session_id=data["session_id"],
            segment_id=data["segment_id"],
            noun=data["noun"],
            verb=data["verb"],
            mood=data.get("mood"),
            engagement=int(data.get("engagement", 0)),
            timestamp_start=data["timestamp_start"],
            timestamp_end=data["timestamp_end"],
            source_hash=data["source_hash"],
            extraction_confidence=float(data.get("extraction_confidence", 0.5)),
        )


def new_tuple_id() -> str:
    return str(uuid4())


# ─────────────────────────────────────────────────────────────────────────────
# Cluster view (aggregate over tuples)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ClusterSpec:
    """How to group tuples when forming clusters.

    Typical shapes:
      - ClusterSpec(group_by=["noun", "verb"])  # full (noun × verb) cells
      - ClusterSpec(group_by=["noun"])          # by domain only
    """

    group_by: list[str] = field(default_factory=lambda: ["noun", "verb"])
    filter: dict[str, str] | None = None  # optional: {field_name: value}


@dataclass
class ClusterCell:
    """Aggregate over one cell of the cluster space."""

    key: tuple  # e.g. ("fitness", "tracking")
    tuple_count: int
    engagement_total: int
    distinct_sessions: int
    mood_distribution: dict[str, int]  # mood → count (None counts as "unspecified")
    earliest: str  # ISO8601
    latest: str  # ISO8601

    @property
    def frustrated_mood_share(self) -> float:
        """Fraction of tuples tagged ``frustrated``."""
        if not self.mood_distribution or self.tuple_count == 0:
            return 0.0
        return self.mood_distribution.get("frustrated", 0) / self.tuple_count

    def to_dict(self) -> dict:
        return {
            "key": list(self.key),
            "tuple_count": self.tuple_count,
            "engagement_total": self.engagement_total,
            "distinct_sessions": self.distinct_sessions,
            "mood_distribution": dict(self.mood_distribution),
            "earliest": self.earliest,
            "latest": self.latest,
        }


@dataclass
class ClusterView:
    """A materialized cluster view over a time window."""

    spec: ClusterSpec
    cells: list[ClusterCell] = field(default_factory=list)

    def __iter__(self):
        return iter(self.cells)

    def __len__(self) -> int:
        return len(self.cells)

    def by_key(self, key: tuple) -> ClusterCell | None:
        for c in self.cells:
            if c.key == key:
                return c
        return None
