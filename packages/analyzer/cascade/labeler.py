"""cascade.labeler — extract ground-truth labels from cascade telemetry spans.

Per internal/spec-tier-cascade-2026-05-26.md § 2.3 Component 1 (Labeler).

The Phase 4 audit-layer learning loop trains on LabeledOutcomes — one
record per session naming whether routing was right (correctly_held,
correctly_escalated, correctly_demoted, should_have_escalated,
should_have_demoted) along with the evidence source.

**Five ground-truth signals** (spec § 2.3 — the load-bearing ones):

  STRONG signals:
    1. UI-chip override events — operator picked Power/Fast mid-session
       = labeled "should have escalated/demoted" by definition
    2. ask_hint_agreed consent — bot asked, user agreed = labeled
       "correctly_escalated" by the same definition; ask_hint that
       expired without agreement = "wasteful_ask"
    3. Struggle-resolution-at-next-tier — escalated, struggle then
       dropped within N turns = "correctly_escalated"

  WEAK / corroborative:
    4. Cost-incident correlation — sessions preceding a cost_burst
       Signal got *something* wrong (often upstream, but informative)
    5. Haiku judge verdict (Phase 3+) — one feature among several,
       NOT the closer

**Phase 2 first-cut scope:** this module ships the LABELER only —
not the variant generator, weight tuner, or calibrator from § 2.3
Components 2-4. The labeler produces LabeledOutcome records to
`{shared_dir}/cascade/labels/<YYYY-MM-DD>.jsonl`; downstream components
read these later.

Implemented signals in v1: signal #1 (UI-chip overrides) + signal #3
(struggle-resolution-at-next-tier). Signal #2 (ask-hint outcomes)
ships when the cascade controller's ask-hint emission goes live in
Phase 3 — without ask-hints in spans, there's nothing to label. The
labeler scaffolds for it; the active code path drops in alongside.

Pure read-and-write module. No Signal emission (labels are persisted
data; the Phase 4 tuner consumes them later). Idempotent: re-running
the labeler on the same span window produces the same labels (within
floating-point noise) — labels carry a session_id key so duplicates
get overwritten on re-run.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable, Iterator, Literal


# ─────────────────────────────────────────────────────────────────────────────
# Label taxonomy
# ─────────────────────────────────────────────────────────────────────────────


class Label(str, Enum):
    """The verdict the tuner consumes."""

    CORRECTLY_HELD = "correctly_held"
    CORRECTLY_ESCALATED = "correctly_escalated"
    CORRECTLY_DEMOTED = "correctly_demoted"
    SHOULD_HAVE_ESCALATED = "should_have_escalated"
    SHOULD_HAVE_DEMOTED = "should_have_demoted"
    WASTEFUL_ESCALATION = "wasteful_escalation"
    WASTEFUL_ASK = "wasteful_ask"
    UNLABELED = "unlabeled"


class LabelSource(str, Enum):
    """Which ground-truth signal produced this label."""

    UI_CHIP_OVERRIDE = "ui_chip_override"        # Signal #1 (STRONG)
    ASK_HINT_AGREED = "ask_hint_agreed"          # Signal #2 (STRONG) — Phase 3
    ASK_HINT_TIMED_OUT = "ask_hint_timed_out"    # Signal #2 (STRONG) — Phase 3
    STRUGGLE_RESOLUTION = "struggle_resolution"  # Signal #3 (STRONG)
    COST_INCIDENT = "cost_incident"              # Signal #4 (WEAK) — Phase 3
    HAIKU_JUDGE = "haiku_judge"                  # Signal #5 (WEAK) — Phase 3+


# ─────────────────────────────────────────────────────────────────────────────
# LabeledOutcome
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class LabeledOutcome:
    """One labeled outcome for the tuner to train on."""

    session_id: str
    bot_id: str | None
    label: Label
    confidence: float                   # 0.0 - 1.0
    source: LabelSource
    # Provenance: which spans + which fields drove the label.
    evidence: dict = field(default_factory=dict)
    # When the outcome was observed (last span's end_time in window).
    observed_at: str = ""               # ISO-8601
    # When the labeler computed it.
    labeled_at: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["label"] = self.label.value
        d["source"] = self.source.value
        return d


# ─────────────────────────────────────────────────────────────────────────────
# Per-session feature accumulator
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class SessionFeatures:
    """Stats accumulated from all spans of a single session — input to
    the labeler's decision rules."""

    session_id: str
    bot_id: str | None = None
    turn_count: int = 0
    # Sequence of (turn_index, tier_used, tier_chosen_by, consent_source,
    # struggle_score, ui_chip_override_observed)
    timeline: list[dict] = field(default_factory=list)
    first_span_at: str = ""
    last_span_at: str = ""
    holdout: bool = False
    holdout_hijacked: bool = False  # holdout-assigned but user overrode mid-session


def _accumulate_session_features(spans: list[dict]) -> SessionFeatures:
    """Build per-session feature record from this session's spans."""
    if not spans:
        return SessionFeatures(session_id="<empty>")

    # Sort by turn_index (stable; fall back to end_time).
    def _key(s: dict) -> tuple[int, str]:
        attrs = s.get("attributes") or {}
        idx = attrs.get("turn_index")
        return (int(idx) if isinstance(idx, int) else 10**9, s.get("end_time") or "")
    sorted_spans = sorted(spans, key=_key)

    first = sorted_spans[0]
    last = sorted_spans[-1]
    first_attrs = first.get("attributes") or {}
    last_attrs = last.get("attributes") or {}

    features = SessionFeatures(
        session_id=first_attrs.get("session_id") or "<no_session_id>",
        bot_id=first.get("bot_id"),
        turn_count=len(sorted_spans),
        first_span_at=first.get("end_time") or "",
        last_span_at=last.get("end_time") or "",
        holdout=bool(first_attrs.get("cascade.holdout")),
    )

    # Build timeline + detect ui_chip override events.
    #
    # ``ui_chip`` is the strongest Signal-#1 ground truth (operator
    # picked Power/Fast on the per-turn chip). It MUST be distinguished
    # from the other two consent_source values (``ask_hint_agreed`` —
    # bot asked and user said yes; ``bot_initiated`` — bot decided
    # without asking) because they are weaker signals and shouldn't
    # corrupt the UI-chip-override label class.
    #
    # The plugin sets ``tier_chosen_by="user_request"`` for ALL three
    # consent_source values, so we cannot use tier_chosen_by alone —
    # the discrimination lives in cascade.consent_source. Earlier code
    # assumed tier_chosen_by="user_request" implied ui_chip; that
    # conflated bot-initiated and ask-hint-agreed calls into the
    # strongest-signal cohort, breaking Phase 4 calibration ground
    # truth.
    saw_ui_chip = False
    saw_non_ui_chip = False
    for sp in sorted_spans:
        attrs = sp.get("attributes") or {}
        chosen_by = attrs.get("cascade.tier_chosen_by")
        consent_source = attrs.get("cascade.consent_source")
        # UI chip = explicit operator pick, the strongest signal.
        # Treat absent consent_source on user_request spans as
        # ui_chip for back-compat (pre-PR-1654 spans had no
        # consent_source field; ui_chip was the only path that
        # set tier_chosen_by=user_request at that time).
        if chosen_by == "user_request" and consent_source in ("ui_chip", None):
            saw_ui_chip = True
        elif chosen_by in ("classifier", "cascade"):
            saw_non_ui_chip = True
        features.timeline.append({
            "turn_index": attrs.get("turn_index"),
            "tier_used": attrs.get("cascade.tier_used"),
            "tier_chosen_by": chosen_by,
            "struggle_score": attrs.get("cascade.struggle.score"),
            "consent_source": consent_source,
        })

    # Hijack detection: session was assigned to holdout, but operator
    # took control via UI chip → exclude from tuner per round-3 #1.
    if features.holdout and saw_ui_chip:
        features.holdout_hijacked = True
        features.holdout = False

    return features


# ─────────────────────────────────────────────────────────────────────────────
# Label-detection rules (the actual ground-truth signals)
# ─────────────────────────────────────────────────────────────────────────────


def _detect_ui_chip_label(features: SessionFeatures) -> LabeledOutcome | None:
    """Signal #1 (STRONG): UI-chip override events.

    Operator picked Power/Fast mid-session = labeled "should have
    escalated/demoted" — the operator's explicit pick IS the ground
    truth that the cascade got it wrong for that session.
    """
    for entry in features.timeline:
        chosen_by = entry.get("tier_chosen_by")
        consent_source = entry.get("consent_source")
        tier = entry.get("tier_used")
        if chosen_by != "user_request":
            continue
        # Discriminate by consent_source: only ui_chip is the
        # strongest-signal UI-chip-override (Signal #1). Skip
        # bot_initiated / ask_hint_agreed — those are weaker signals
        # that belong to other label sources. Treat None as ui_chip
        # for back-compat with pre-consent_source spans.
        if consent_source not in ("ui_chip", None):
            continue
        if tier == "tier1":
            return LabeledOutcome(
                session_id=features.session_id,
                bot_id=features.bot_id,
                label=Label.SHOULD_HAVE_ESCALATED,
                confidence=0.9,
                source=LabelSource.UI_CHIP_OVERRIDE,
                evidence={
                    "turn_index": entry.get("turn_index"),
                    "chosen_tier": tier,
                },
                observed_at=features.last_span_at,
            )
        if tier == "tier3":
            return LabeledOutcome(
                session_id=features.session_id,
                bot_id=features.bot_id,
                label=Label.SHOULD_HAVE_DEMOTED,
                confidence=0.9,
                source=LabelSource.UI_CHIP_OVERRIDE,
                evidence={
                    "turn_index": entry.get("turn_index"),
                    "chosen_tier": tier,
                },
                observed_at=features.last_span_at,
            )
    return None


def _detect_struggle_resolution_label(features: SessionFeatures) -> LabeledOutcome | None:
    """Signal #3 (STRONG): struggle-resolution-at-next-tier.

    A session that sat at tier T with sustained struggle, escalated to
    T+1, and then struggle dropped within 2 turns at T+1 → labeled
    "correctly_escalated."

    Conservative: requires at least 2 high-struggle turns at the old
    tier AND at least 2 low-struggle turns at the new tier.
    """
    timeline = features.timeline
    if len(timeline) < 4:
        return None

    # Find an escalation event in the timeline (tier change upward).
    tier_rank = {"tier3": 0, "tier2": 1, "tier1": 2}
    for i in range(1, len(timeline)):
        prev_tier = timeline[i - 1].get("tier_used")
        cur_tier = timeline[i].get("tier_used")
        if prev_tier not in tier_rank or cur_tier not in tier_rank:
            continue
        if tier_rank[cur_tier] <= tier_rank[prev_tier]:
            continue  # not an escalation

        # Check the turns BEFORE escalation for sustained struggle (≥ 2).
        before = [timeline[j] for j in range(max(0, i - 3), i)]
        high_before = [
            t for t in before
            if isinstance(t.get("struggle_score"), (int, float))
            and t.get("struggle_score") > 0.5
        ]
        if len(high_before) < 2:
            continue

        # Check the turns AFTER escalation for low struggle (≥ 2).
        after = timeline[i:i + 3]
        low_after = [
            t for t in after
            if isinstance(t.get("struggle_score"), (int, float))
            and t.get("struggle_score") < 0.3
        ]
        if len(low_after) < 2:
            continue

        # Good signal — escalation resolved struggle.
        return LabeledOutcome(
            session_id=features.session_id,
            bot_id=features.bot_id,
            label=Label.CORRECTLY_ESCALATED,
            confidence=0.75,
            source=LabelSource.STRUGGLE_RESOLUTION,
            evidence={
                "escalation_turn": timeline[i].get("turn_index"),
                "from_tier": prev_tier,
                "to_tier": cur_tier,
                "high_struggle_turns_before": len(high_before),
                "low_struggle_turns_after": len(low_after),
            },
            observed_at=features.last_span_at,
        )
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Top-level label() function
# ─────────────────────────────────────────────────────────────────────────────


def label_session(features: SessionFeatures) -> LabeledOutcome | None:
    """Apply each ground-truth signal in priority order. Returns the
    highest-confidence label, or None if no signal fires.

    Phase 2 first-cut: only signals #1 (UI-chip override) and #3
    (struggle-resolution) are implemented. Signals #2, #4, #5 ship in
    Phase 3+.
    """
    # Hijacked holdouts are excluded entirely per round-3 #1.
    # (They're useful for OTHER analysis but contaminated for the tuner.)
    if features.holdout_hijacked:
        return None

    # Try strong signals in priority order.
    label = _detect_ui_chip_label(features)
    if label is not None:
        return label

    label = _detect_struggle_resolution_label(features)
    if label is not None:
        return label

    return None


def label_spans(spans: Iterable[dict]) -> Iterator[LabeledOutcome]:
    """Group spans by session_id and emit a LabeledOutcome per labeled session.

    Sessions that don't fire any ground-truth signal are skipped (NOT
    yielded as `Label.UNLABELED`) — the tuner only needs the labeled
    subset.
    """
    by_session: dict[str, list[dict]] = defaultdict(list)
    for span in spans:
        attrs = span.get("attributes") or {}
        sid = attrs.get("session_id")
        if isinstance(sid, str):
            by_session[sid].append(span)

    now_iso = datetime.now(timezone.utc).isoformat()
    for sid, session_spans in by_session.items():
        features = _accumulate_session_features(session_spans)
        outcome = label_session(features)
        if outcome is None:
            continue
        if not outcome.labeled_at:
            outcome.labeled_at = now_iso
        yield outcome


# ─────────────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────────────


def write_labels(
    outcomes: Iterable[LabeledOutcome],
    shared_dir: Path,
    *,
    day: str | None = None,
) -> dict:
    """Write labeled outcomes to {shared_dir}/cascade/labels/<YYYY-MM-DD>.jsonl.

    Dedup-on-write, keyed by session_id: the labeler runs on an overlapping
    ~65-minute window, so the same session is re-labeled across runs. Rather
    than appending duplicate rows the (not-yet-built) tuner would have to
    dedup, we merge today's existing rows with the incoming ones — one record
    per session, latest wins — and rewrite the file atomically. Combined with
    the daily retention cron this keeps the labels store bounded.

    Returns a small report dict: {written, path}.
    """
    if day is None:
        day = datetime.now(timezone.utc).date().isoformat()

    labels_dir = shared_dir / "cascade" / "labels"
    labels_dir.mkdir(parents=True, exist_ok=True)
    path = labels_dir / f"{day}.jsonl"

    # Existing rows keyed by session_id (insertion order preserved).
    by_session: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                sid = json.loads(line).get("session_id")
            except json.JSONDecodeError:
                continue
            if sid is not None:
                by_session[sid] = line

    written = 0
    for outcome in outcomes:
        d = outcome.to_dict()
        by_session[outcome.session_id] = json.dumps(d, separators=(",", ":"))
        written += 1

    body = "\n".join(by_session.values())
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text(body + "\n" if body else "", encoding="utf-8")
    tmp.rename(path)

    return {"written": written, "path": str(path)}
