"""severity — vector / magnitude / fix-risk framework for Signals + Proposals.

Replaces the ad-hoc per-producer ``info | warn | alert`` policy with a
structured (vector, magnitude) tag on the *finding* plus a separate
fix-risk attribute on the *action*. Composes into a derived priority
score used for narrative ordering and (alongside decidability) the
tier-c authority gate.

Spec: internal/spec-severity-framework-2026-05-18.md

Three axes, all independent:

  * **Severity** (vector × magnitude) — property of the finding
  * **Decidability** — property of the action (is there a clear right answer)
  * **Fix-risk** — property of the action (how bad if the fix itself goes wrong)

This module owns the severity axis + the priority composition. Decidability
and fix-risk live alongside as enums but their classifier rules live with
the dispatcher (later phase).

Backward compat: producers that haven't been retrofitted to emit
``vector`` + ``magnitude`` in their Signal details fall through to the
defaults in :func:`infer_magnitude` (driven by the legacy ``severity``
field) and :data:`DEFAULT_VECTOR_FOR_PRODUCER`. The Home renderer and
Alerts ordering work in mixed mode — gradual migration is the design.
"""

from __future__ import annotations

from typing import Literal, Mapping, NamedTuple


# ─────────────────────────────────────────────────────────────────────────────
# Types
# ─────────────────────────────────────────────────────────────────────────────


Vector = Literal["security", "cost", "operations", "quality"]
ALL_VECTORS: tuple[Vector, ...] = ("security", "cost", "operations", "quality")

# Magnitude is a 0-4 anchored ordinal. We type it loosely as int (rather
# than Literal[0,1,2,3,4]) so producers that compute magnitude
# programmatically don't have to cast — `clamp_magnitude` handles
# defensive bounding.
Magnitude = int

# Fix-risk lives on the action / remediation, NOT on the finding. Producers
# that emit a structured remediation should tag this; default is "medium"
# when omitted (conservative).
FixRisk = Literal["low", "medium", "high"]
ALL_FIX_RISKS: tuple[FixRisk, ...] = ("low", "medium", "high")

# Scope drives the impact multiplier in priority composition. Mirrors
# the Signal.scope literal from schema/signal.py.
Scope = Literal["pod", "bot", "host", "integration"]


class SeverityRating(NamedTuple):
    """A producer's severity tag on a finding.

    ``note`` is optional human-readable rationale, useful when a producer
    diverged from its usual magnitude for a specific case.
    """

    vector: Vector
    magnitude: Magnitude
    note: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Anchor cards — canonical examples per (vector, magnitude)
# ─────────────────────────────────────────────────────────────────────────────
# Source of truth for cross-producer calibration. When in doubt, a
# producer cites the closest anchor when tagging a finding. The cards
# also drive the operator-facing display ("Why is this magnitude 3?")
# and unit-test asserts on canonical fixtures.
#
# Mirrors the tables in internal/spec-severity-framework-2026-05-18.md §2.
# Edit both this dict and the spec doc when adding examples — they
# should not drift.


SEVERITY_ANCHORS: dict[Vector, dict[Magnitude, str]] = {
    "security": {
        0: "Advisory only, no real risk. Audit notes a permissive policy on a read-only tool.",
        1: "Theoretical exposure, mitigations in place. Coding agent exec policy is `scoped` rather than `minimal`.",
        2: "Real exposure, scoped to one bot. .zshrc unreadable (auth state may have leaked into shell init).",
        3: "Active exploitation possible, OR a token leak with limited scope. Partial-scope GitHub OAuth token in a checked-in file.",
        4: "Catastrophic — token leaked at scale, auth bypass, data exfil. Anthropic API key in chat logs, exposed >1h.",
    },
    "cost": {
        0: "Negligible (<$1/day impact). One session burned $0.30 extra.",
        1: "Trending up but contained. Daily spend ticked from $2 to $3 baseline.",
        2: "Real overrun: $5-25/day, OR trajectory hits cap in >14d. Bot consistently at $15/day vs $5 baseline.",
        3: "Material overrun: $25-100/day, OR cap hit in 7-14d. Embedding 429 storm burning fallback API calls.",
        4: "Severe overrun: $100+/day, OR cap hit now. Pod-wide spend 3× expected; cap actively gating turns.",
    },
    "operations": {
        0: "Cosmetic, no functional impact. Optional plugin at N-1.",
        1: "Single bot degraded but functioning. One bot's metrics file is 30 min stale.",
        2: "Single bot down OR pod-wide degraded. Member bot's gateway unreachable for 15 min.",
        3: "Pod-wide degraded OR multi-bot down. Two member gateways down simultaneously.",
        4: "Pod-wide hard outage. Admin UI unreachable. Shared dir read-only.",
    },
    "quality": {
        0: "Style nit. A formatting inconsistency.",
        1: "Minor UX. Classifier accuracy dipped 3%.",
        2: "Noticeable UX or quality regression. Cache invalidation hurting hit rate 20%+.",
        3: "Major regression, users actively affected. Bot keeps generating broken tool calls.",
        4: "Catastrophic — bot is actively harmful or broken. Wrong response to wrong user.",
    },
}


def anchor_text(vector: Vector, magnitude: Magnitude) -> str:
    """Return the canonical anchor for a (vector, magnitude). Empty
    string if the magnitude is out of the [0,4] range — producers
    pre-clamp via :func:`clamp_magnitude` to avoid that."""
    return SEVERITY_ANCHORS.get(vector, {}).get(magnitude, "")


# ─────────────────────────────────────────────────────────────────────────────
# Default vector by producer — for the backward-compat fall-through
# ─────────────────────────────────────────────────────────────────────────────
# When a producer hasn't been retrofitted to emit explicit vector +
# magnitude on its Signals, we infer. The vector inference is by
# producer (each producer is roughly single-domain); the magnitude
# inference is by the producer's existing `severity` field (info/warn/
# alert).
#
# Producers missing here fall through to `_DEFAULT_VECTOR_FALLBACK`.
# New producers should get an explicit entry here in the same PR that
# registers them in `signals/producer_severity.py`.


DEFAULT_VECTOR_FOR_PRODUCER: Mapping[str, Vector] = {
    # ── security ──────────────────────────────────────────────────────────
    "security_warden": "security",
    "audit": "security",
    # ── cost ──────────────────────────────────────────────────────────────
    "cost_watchdog": "cost",
    "session_economics": "cost",
    "embedding_monitor": "cost",
    # ── operations ────────────────────────────────────────────────────────
    "heal": "operations",
    "sysadmin_watchdog": "operations",
    "pod_report": "operations",
    "bot_log_monitor": "operations",
    "integration_probe": "operations",
    "evolve_watchdog": "operations",
    "alerts_loop_monitor": "operations",
    "deploy_drift_monitor": "operations",
    "bot_recovery_monitor": "operations",
    # ── quality ───────────────────────────────────────────────────────────
    "proposal_synthesizer": "quality",
}

_DEFAULT_VECTOR_FALLBACK: Vector = "operations"


def default_vector_for_producer(producer: str) -> Vector:
    """Return the conventional vector for a producer. Conservative
    fallback to `operations` for unknown producers — most generic
    findings without a domain tag are operational."""
    return DEFAULT_VECTOR_FOR_PRODUCER.get(producer, _DEFAULT_VECTOR_FALLBACK)


# ─────────────────────────────────────────────────────────────────────────────
# Magnitude inference from legacy `severity` field
# ─────────────────────────────────────────────────────────────────────────────
# Bridges producers that haven't been retrofitted. Conservative on the
# upside (warn → 2 not 3) so the priority order doesn't false-elevate
# legacy producers above retrofitted ones with clearly-anchored
# magnitudes.


_LEGACY_SEVERITY_TO_MAGNITUDE: Mapping[str, Magnitude] = {
    "info": 1,
    "warn": 2,
    "alert": 4,
}


def infer_magnitude(legacy_severity: str | None) -> Magnitude:
    """Infer a magnitude from a producer's legacy severity field.

    Conservative mapping: info → 1, warn → 2, alert → 4. Unknown or
    None falls through to 2 (the same default the Signal store uses
    when severity is omitted).
    """
    if not legacy_severity:
        return 2
    return _LEGACY_SEVERITY_TO_MAGNITUDE.get(legacy_severity.lower(), 2)


def clamp_magnitude(value: int) -> Magnitude:
    """Bound a magnitude to the [0, 4] anchored range. Producers that
    compute magnitude programmatically should pre-clamp; this is the
    canonical clamp."""
    if value < 0:
        return 0
    if value > 4:
        return 4
    return value


# ─────────────────────────────────────────────────────────────────────────────
# Resolution — pull (vector, magnitude) off a Signal-shaped dict
# ─────────────────────────────────────────────────────────────────────────────
# Producers tag explicitly via signal.details:
#
#   {"vector": "security", "magnitude": 3}
#
# Or omit and let the resolver fall through to producer-default vector +
# legacy-severity-inferred magnitude. Either way, callers (the Home
# narrative renderer, the Alerts page sort, the tier-c gate) get a
# normalized (vector, magnitude) pair.


def resolve_severity(signal_or_dict: dict | object) -> SeverityRating:
    """Resolve a SeverityRating from a Signal (or Signal.to_dict() shape).

    Looks at ``details.vector`` + ``details.magnitude`` first (the
    explicit producer tag), then falls back to producer-default vector +
    legacy-severity-inferred magnitude. Always returns a valid rating —
    never raises on missing/malformed input.
    """
    # Accept either a Signal dataclass or its dict form. Test the dict
    # path first since it's the more common caller (the admin UI
    # already gets Signals via to_dict() through the API).
    if isinstance(signal_or_dict, dict):
        details = signal_or_dict.get("details") or {}
        producer = signal_or_dict.get("producer") or ""
        legacy_sev = signal_or_dict.get("severity")
    else:
        details = getattr(signal_or_dict, "details", {}) or {}
        producer = getattr(signal_or_dict, "producer", "") or ""
        legacy_sev = getattr(signal_or_dict, "severity", None)

    explicit_vector = details.get("vector") if isinstance(details, dict) else None
    explicit_mag = details.get("magnitude") if isinstance(details, dict) else None

    if explicit_vector in ALL_VECTORS:
        vector: Vector = explicit_vector  # type: ignore[assignment]
    else:
        vector = default_vector_for_producer(producer)

    if isinstance(explicit_mag, int):
        magnitude = clamp_magnitude(explicit_mag)
    elif isinstance(explicit_mag, float) and not isinstance(explicit_mag, bool):
        magnitude = clamp_magnitude(int(round(explicit_mag)))
    else:
        magnitude = infer_magnitude(legacy_sev)

    note = ""
    if isinstance(details, dict):
        raw_note = details.get("severity_note") or details.get("magnitude_note")
        if isinstance(raw_note, str):
            note = raw_note

    return SeverityRating(vector=vector, magnitude=magnitude, note=note)


# ─────────────────────────────────────────────────────────────────────────────
# Priority composition
# ─────────────────────────────────────────────────────────────────────────────
# Single number the UI orders by. Derived, never producer-supplied.
# Spec: §4 of internal/spec-severity-framework-2026-05-18.md
#
# Range: ~0.0 to ~15.6 (max = 4 × 1.5 × 1.3 × 2.0).
#
# Thresholds — see also _UI_THRESHOLDS below for the narrative-bucket
# split. Documented in the spec; codified here so consumers don't
# scatter magic numbers.


IMPACT_MULTIPLIER: Mapping[Scope, float] = {
    "pod": 1.5,
    "host": 1.4,
    "integration": 1.3,
    "bot": 1.0,
}

URGENCY_ACTIVE_OUTAGE: float = 1.3
URGENCY_SELF_RESOLVING: float = 0.7

# Default operator weights. Operator overrides via
# network.json::severity_weights.{vector} — see :func:`resolve_pod_weights`.
DEFAULT_POD_WEIGHTS: Mapping[Vector, float] = {
    "security": 1.0,
    "cost": 1.0,
    "operations": 1.0,
    "quality": 1.0,
}

# UI thresholds, named so callers reference symbols rather than literals.
# Calibration: the realistic max without operator weight tuning is
# magnitude 4 × pod (1.5) × active_outage (1.3) = 7.8. The lead
# threshold sits just below that so a pod-wide critical with active
# outage qualifies; finer urgency lives in operator-tunable weights.
PRIORITY_LEAD_THRESHOLD: float = 7.0
PRIORITY_NARRATIVE_THRESHOLD: float = 3.0


def resolve_pod_weights(network: dict | None) -> Mapping[Vector, float]:
    """Read operator-tunable pod weights from network.json.

    ``network.json::severity_weights`` — a dict mapping vector → float.
    Missing keys fall back to 1.0. Invalid values (non-positive,
    non-numeric) are silently ignored — defensive, since this is
    operator-edited config.
    """
    out: dict[Vector, float] = dict(DEFAULT_POD_WEIGHTS)
    if not isinstance(network, dict):
        return out
    raw = network.get("severity_weights")
    if not isinstance(raw, dict):
        return out
    for v in ALL_VECTORS:
        val = raw.get(v)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
            out[v] = float(val)
    return out


def compose_priority(
    rating: SeverityRating,
    *,
    scope: Scope = "bot",
    is_active_outage: bool = False,
    is_self_resolving: bool = False,
    pod_weights: Mapping[Vector, float] | None = None,
) -> float:
    """Compose a priority score from a severity rating + context.

    Returns a float in roughly [0.0, 15.6]. Use :func:`priority_bucket`
    to convert to the narrative-bucket label.

    Parameters
    ----------
    rating
        The severity rating produced by :func:`resolve_severity`.
    scope
        Signal scope; drives the impact multiplier.
    is_active_outage
        Set ``True`` when the underlying condition is causing an
        in-progress outage (a gateway is *currently* unreachable, not
        just was a few minutes ago). Adds 30% urgency.
    is_self_resolving
        Set ``True`` for findings that historically clear without
        intervention (transient embedding 429s, brief metrics
        staleness). Discounts 30%.
    pod_weights
        Operator-tunable per-vector multipliers (see
        :func:`resolve_pod_weights`). Defaults to all-1.0.
    """
    base = float(rating.magnitude)
    impact = IMPACT_MULTIPLIER.get(scope, 1.0)
    urgency = 1.0
    if is_active_outage:
        urgency *= URGENCY_ACTIVE_OUTAGE
    if is_self_resolving:
        urgency *= URGENCY_SELF_RESOLVING
    weights = pod_weights or DEFAULT_POD_WEIGHTS
    weight = weights.get(rating.vector, 1.0)
    return base * impact * urgency * weight


PriorityBucket = Literal["lead", "in_narrative", "small"]


def priority_bucket(priority: float) -> PriorityBucket:
    """Map a composed priority score to a narrative bucket label.

    Used by the Home renderer to decide what leads the narrative, what
    appears below the lead, and what collapses into the "smaller stuff"
    section. Thresholds are the named constants above.
    """
    if priority >= PRIORITY_LEAD_THRESHOLD:
        return "lead"
    if priority >= PRIORITY_NARRATIVE_THRESHOLD:
        return "in_narrative"
    return "small"


# ─────────────────────────────────────────────────────────────────────────────
# Fix-risk helpers
# ─────────────────────────────────────────────────────────────────────────────


def normalize_fix_risk(value: str | None) -> FixRisk:
    """Bound a fix-risk string to the supported enum. Unknown / missing
    values default to ``"medium"`` — conservative, so unclassified
    actions never auto-fire."""
    if value in ALL_FIX_RISKS:
        return value  # type: ignore[return-value]
    return "medium"
