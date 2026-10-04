"""generators.gateway_diagnostician.observe — Heal-config diagnostician.

Reads heal's incident stream for the target bot, applies a small heuristic
set, and emits proposals to tune heal's configuration when the evidence
supports a specific change.

L1 ships one detector:
  - ``health_timeout_too_low`` — if recent gateway_down incidents are
    dominated by timeout-shaped errors and the per-attempt timeout is
    still below the ceiling, propose raising it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from proposal_synthesizer.emit import emit_from_proposal
from schema.candidate_proposal import Magnitude
from schema.proposal import (
    Claim,
    Proposal,
    Provenance,
    RiskTag,
    WorkflowInstruction,
    new_proposal_id,
)


GENERATOR_ID = "gateway_diagnostician"
DIMENSION = "substrate_health"


def _variant_for_proposal(p: Proposal) -> str:
    """Map a proposal back to its detector for the candidate's variant."""
    tech = (p.provenance.technique or "").split(".", 1)[-1]
    return tech or "gateway_finding"


# ─────────────────────────────────────────────────────────────────────────────
# Context
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class GatewayDiagnosticianContext:
    """Inputs the diagnostician's detectors need.

    ``heal_timeout_seconds`` and ``slow_threshold_ms`` are the *currently
    configured* values for this bot (per-bot override if set, otherwise the
    pod-wide heal default). Without them the detectors cannot tell whether
    a proposed change is redundant.
    """

    bot_id: str
    shared_dir: Path
    heal_timeout_seconds: int
    slow_threshold_ms: int = 3000
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Tunables (read from generator config when present, else defaults).
    # Window is shared by every detector in this generator; per-detector
    # thresholds are deliberately separate so operators can tune them
    # independently.
    window_days: int = 7

    # health_timeout_too_low detector tunables
    min_incidents: int = 5
    timeout_share_threshold: float = 0.5
    timeout_ceiling_seconds: int = 60
    proposed_increment_seconds: int = 15

    # slow_threshold_too_low detector tunables
    min_slow_incidents: int = 10
    max_down_for_slow_diagnosis: int = 2
    slow_threshold_ceiling_ms: int = 10000
    slow_threshold_margin_factor: float = 1.2
    slow_threshold_headroom_ms: int = 500

    audience: str = "pod_operator"


# ─────────────────────────────────────────────────────────────────────────────
# Incident loader
# ─────────────────────────────────────────────────────────────────────────────


# Substrings we treat as timeout-shaped failures. heal.py logs these in the
# incident's ``detail`` field when oc_health hits its subprocess timeout or
# when the gateway closes the WebSocket abnormally during plugin churn.
_TIMEOUT_PATTERNS = (
    "timeout",
    "timed out",
    "timedout",
    "abnormal closure",
    "1006",
    "websocket",
    "subprocess timeout",
    "TimeoutExpired",
)


def _looks_like_timeout(detail: str) -> bool:
    if not detail:
        return False
    lower = detail.lower()
    return any(p in lower for p in _TIMEOUT_PATTERNS)


def _read_recent_incidents(
    shared_dir: Path,
    bot_id: str,
    since: datetime,
    until: datetime,
    *,
    types: tuple[str, ...] = ("gateway_down",),
) -> list[dict]:
    """Return incident records for ``bot_id`` in [since, until], filtered to
    the supplied ``types``. Defaults to ``gateway_down`` so callers that
    don't pass ``types`` keep the original behavior."""
    incidents_dir = shared_dir / "incidents"
    if not incidents_dir.exists():
        return []
    type_set = set(types)
    out: list[dict] = []
    for day_dir in incidents_dir.iterdir():
        if not day_dir.is_dir():
            continue
        for f in day_dir.glob(f"{bot_id}-*.json"):
            try:
                inc = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if inc.get("type") not in type_set:
                continue
            ts_raw = inc.get("detected_at", "")
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if ts < since or ts > until:
                continue
            out.append(inc)
    return out


def _median(values: list[float]) -> float:
    """Median of a non-empty numeric list. (Avoids importing statistics for
    a one-shot use in this module.)"""
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


# ─────────────────────────────────────────────────────────────────────────────
# Detector: health_timeout_too_low
# ─────────────────────────────────────────────────────────────────────────────


def _detect_health_timeout_too_low(ctx: GatewayDiagnosticianContext) -> Proposal | None:
    """Raise heal.ocHealthTimeoutSeconds when recent gateway_down incidents
    look like timeouts and the current value is still below the ceiling."""
    if ctx.heal_timeout_seconds >= ctx.timeout_ceiling_seconds:
        # Already at or above the ceiling; nothing to propose.
        return None

    since = ctx.now - timedelta(days=ctx.window_days)
    incidents = _read_recent_incidents(ctx.shared_dir, ctx.bot_id, since, ctx.now)
    if len(incidents) < ctx.min_incidents:
        # Not enough data to be confident the pattern is real.
        return None

    timeout_count = sum(1 for inc in incidents if _looks_like_timeout(inc.get("detail", "")))
    timeout_share = timeout_count / len(incidents)
    if timeout_share < ctx.timeout_share_threshold:
        return None

    proposed_value = min(
        ctx.heal_timeout_seconds + ctx.proposed_increment_seconds,
        ctx.timeout_ceiling_seconds,
    )
    if proposed_value <= ctx.heal_timeout_seconds:
        return None  # nothing to gain

    summary = (
        f"Raise heal probe timeout for {ctx.bot_id}: "
        f"{timeout_count}/{len(incidents)} recent gateway_down incidents "
        f"({timeout_share:.0%}) look like timeouts; "
        f"current ocHealthTimeoutSeconds={ctx.heal_timeout_seconds}s, "
        f"propose {proposed_value}s."
    )

    instructions = (
        f"# Raise heal probe timeout for `{ctx.bot_id}`\n"
        f"\n"
        f"## Evidence\n"
        f"\n"
        f"- Window: last {ctx.window_days} days\n"
        f"- gateway_down incidents: **{len(incidents)}**\n"
        f"- Of which timeout-shaped: **{timeout_count} ({timeout_share:.0%})**\n"
        f"  (matched on detail substrings: timeout, abnormal closure, 1006, …)\n"
        f"- Current `ocHealthTimeoutSeconds`: **{ctx.heal_timeout_seconds}s**\n"
        f"- Proposed: **{proposed_value}s**\n"
        f"\n"
        f"## Why\n"
        f"\n"
        f"heal.py marks a gateway as down when `oc_health` exits without a\n"
        f"successful response inside the configured timeout. Heavy bots can\n"
        f"legitimately take longer than the default — heal.py already runs\n"
        f"each bot through `_resolve_oc_health_timeout`, which honors a\n"
        f"per-bot override at `network.bots[\"{ctx.bot_id}\"].heal.ocHealthTimeoutSeconds`.\n"
        f"The recent incident pattern suggests the current value is too low\n"
        f"for this bot.\n"
        f"\n"
        f"## Apply\n"
        f"\n"
        f"Edit `/Users/Shared/evolve/network.json` and set the per-bot\n"
        f"override:\n"
        f"\n"
        f"```json\n"
        f"\"bots\": {{\n"
        f"  \"{ctx.bot_id}\": {{\n"
        f"    \"heal\": {{\n"
        f"      \"ocHealthTimeoutSeconds\": {proposed_value}\n"
        f"    }}\n"
        f"  }}\n"
        f"}}\n"
        f"```\n"
        f"\n"
        f"heal.py picks the new value up on its next 5-minute cycle.\n"
        f"\n"
        f"## Verify\n"
        f"\n"
        f"After 7 days, gateway.consecutive_failures_24h for `{ctx.bot_id}`\n"
        f"should drop. If it does not, revert and investigate further —\n"
        f"the gateway may be wedged below heal's reach rather than slow.\n"
    )

    claim = Claim(
        metric="gateway.consecutive_failures_24h",
        direction="down",
        magnitude=max(1.0, len(incidents) / max(1, ctx.window_days)),
        window_days=7,
        baseline=float(len(incidents)),
        fallback="revert",
    )

    return Proposal(
        id=new_proposal_id(),
        bot_id=ctx.bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[
            f"health_timeout_too_low:{ctx.bot_id}:"
            f"{ctx.heal_timeout_seconds}->{proposed_value}",
        ],
        provenance=Provenance(
            technique=f"{GENERATOR_ID}.health_timeout_too_low",
            signals={
                "incidents_in_window": len(incidents),
                "timeout_share": round(timeout_share, 3),
                "current_seconds": ctx.heal_timeout_seconds,
                "proposed_seconds": proposed_value,
                "window_days": ctx.window_days,
            },
            confidence=0.85,
        ),
        problem=summary,
        action=WorkflowInstruction(
            bot_id=ctx.bot_id,
            path=f"evolve/heal-config-recommendations/{ctx.bot_id}-raise-timeout.md",
            content=instructions,
        ),
        risk_tag=RiskTag(
            blast_radius="bot",
            reversibility="auto",  # operator can drop the override to revert
            touches=["heal_config"],
        ),
        claim=claim,
        approval_audience=ctx.audience,  # type: ignore[arg-type]
        urgency="improvement",
        admin_surface_summary=summary[:120],
        dismiss_signature="gateway_diagnostician:health_timeout_too_low",
        dismiss_scope="kind",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Detector: slow_threshold_too_low
# ─────────────────────────────────────────────────────────────────────────────


def _detect_slow_threshold_too_low(ctx: GatewayDiagnosticianContext) -> Proposal | None:
    """Raise heal.slowThresholdMs when a bot routinely produces slow probes
    that aren't actually failing.

    Targets the noise-floor case: a heavy bot whose normal response time
    sits comfortably above the default 3000ms threshold but who is still
    meeting heal's actual probe deadline. Every cycle records a noisy
    ``gateway_slow`` incident that pollutes the diagnostician's own input
    stream. Raising the threshold to a calibrated value (median observed
    response + headroom) reduces noise without weakening real failure
    detection — a true outage still trips ``gateway_down`` regardless of
    where the slow line sits.
    """
    if ctx.slow_threshold_ms >= ctx.slow_threshold_ceiling_ms:
        return None  # already at or above ceiling

    since = ctx.now - timedelta(days=ctx.window_days)
    slow = _read_recent_incidents(
        ctx.shared_dir, ctx.bot_id, since, ctx.now, types=("gateway_slow",)
    )
    if len(slow) < ctx.min_slow_incidents:
        return None  # not enough samples

    # Don't diagnose "noise floor" if the bot is actually failing — slow
    # incidents alongside many gateway_down events probably mean something
    # is degrading, not that the threshold is too tight.
    down = _read_recent_incidents(
        ctx.shared_dir, ctx.bot_id, since, ctx.now, types=("gateway_down",)
    )
    if len(down) > ctx.max_down_for_slow_diagnosis:
        return None

    response_times = [
        float(inc["response_time_ms"])
        for inc in slow
        if isinstance(inc.get("response_time_ms"), (int, float))
    ]
    if len(response_times) < ctx.min_slow_incidents:
        return None  # records lacked the response_time_ms field

    median_ms = _median(response_times)
    # Margin gate: only propose if the median is meaningfully above the
    # current threshold, not just barely. Avoids noisy churn when the bot
    # is right at the line.
    if median_ms < ctx.slow_threshold_ms * ctx.slow_threshold_margin_factor:
        return None

    proposed_value = int(min(
        median_ms + ctx.slow_threshold_headroom_ms,
        ctx.slow_threshold_ceiling_ms,
    ))
    if proposed_value <= ctx.slow_threshold_ms:
        return None  # nothing to gain

    summary = (
        f"Raise slow-probe threshold for {ctx.bot_id}: "
        f"{len(slow)} slow incidents in the last {ctx.window_days}d "
        f"(median {median_ms:.0f}ms, current threshold "
        f"{ctx.slow_threshold_ms}ms); propose {proposed_value}ms."
    )

    instructions = (
        f"# Raise heal slow-probe threshold for `{ctx.bot_id}`\n"
        f"\n"
        f"## Evidence\n"
        f"\n"
        f"- Window: last {ctx.window_days} days\n"
        f"- gateway_slow incidents: **{len(slow)}**\n"
        f"- gateway_down incidents in same window: **{len(down)}** "
        f"(this proposal only fires when down-count is low — actual "
        f"failures get a different diagnosis)\n"
        f"- Median response time of slow incidents: **{median_ms:.0f}ms**\n"
        f"- Current `slowThresholdMs`: **{ctx.slow_threshold_ms}ms**\n"
        f"- Proposed: **{proposed_value}ms**\n"
        f"\n"
        f"## Why\n"
        f"\n"
        f"heal.py logs a `gateway_slow` incident whenever a probe succeeds\n"
        f"but takes longer than `slowThresholdMs`. It does not act on the\n"
        f"incident — slow only generates log noise — but the noise pollutes\n"
        f"the diagnostician's own input stream and makes it harder for an\n"
        f"operator to tell real degradation from a heavy-but-healthy bot.\n"
        f"This bot's typical response time is well above the current line\n"
        f"without crossing into actual failure; calibrating the threshold\n"
        f"to its real floor + headroom restores signal-to-noise without\n"
        f"hiding genuine outages (those still trip `gateway_down`).\n"
        f"\n"
        f"## Apply\n"
        f"\n"
        f"Edit `/Users/Shared/evolve/network.json` and set the per-bot\n"
        f"override:\n"
        f"\n"
        f"```json\n"
        f"\"bots\": {{\n"
        f"  \"{ctx.bot_id}\": {{\n"
        f"    \"heal\": {{\n"
        f"      \"slowThresholdMs\": {proposed_value}\n"
        f"    }}\n"
        f"  }}\n"
        f"}}\n"
        f"```\n"
        f"\n"
        f"heal.py picks the new value up on its next 5-minute cycle.\n"
        f"\n"
        f"## Verify\n"
        f"\n"
        f"After 7 days, gateway.slow_incidents_24h for `{ctx.bot_id}` should\n"
        f"drop sharply. If gateway.consecutive_failures_24h rises, revert —\n"
        f"the threshold was masking real degradation.\n"
    )

    claim = Claim(
        metric="gateway.slow_incidents_24h",
        direction="down",
        magnitude=max(1.0, len(slow) / max(1, ctx.window_days)),
        window_days=7,
        baseline=float(len(slow)),
        fallback="revert",
    )

    return Proposal(
        id=new_proposal_id(),
        bot_id=ctx.bot_id,
        generator_id=GENERATOR_ID,
        dimension=DIMENSION,
        trigger_observations=[
            f"slow_threshold_too_low:{ctx.bot_id}:"
            f"{ctx.slow_threshold_ms}->{proposed_value}",
        ],
        provenance=Provenance(
            technique=f"{GENERATOR_ID}.slow_threshold_too_low",
            signals={
                "slow_incidents_in_window": len(slow),
                "down_incidents_in_window": len(down),
                "median_response_ms": round(median_ms, 1),
                "current_threshold_ms": ctx.slow_threshold_ms,
                "proposed_threshold_ms": proposed_value,
                "window_days": ctx.window_days,
            },
            confidence=0.8,
        ),
        problem=summary,
        action=WorkflowInstruction(
            bot_id=ctx.bot_id,
            path=f"evolve/heal-config-recommendations/{ctx.bot_id}-raise-slow-threshold.md",
            content=instructions,
        ),
        risk_tag=RiskTag(
            blast_radius="bot",
            reversibility="auto",
            touches=["heal_config"],
        ),
        claim=claim,
        approval_audience=ctx.audience,  # type: ignore[arg-type]
        urgency="hygiene",  # noise reduction, not a failure mode
        admin_surface_summary=summary[:120],
        dismiss_signature="gateway_diagnostician:slow_threshold_too_low",
        dismiss_scope="kind",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────


def observe(ctx: GatewayDiagnosticianContext) -> list[Proposal]:
    """Run all diagnostician detectors against ``ctx``.

    Phase 6c cutover: emits CandidateProposals only; ``observe()``
    returns ``[]``. Tests inspect ``candidates/pending/``.
    """
    for detector in (_detect_health_timeout_too_low, _detect_slow_threshold_too_low):
        p = detector(ctx)
        if p is not None:
            incidents = float(
                p.provenance.signals.get("recent_incident_count", 0) or 0
            )
            emit_from_proposal(
                p,
                variant=_variant_for_proposal(p),
                magnitude=Magnitude(unit="count", value=incidents or 1.0),
                shared_dir=ctx.shared_dir,
            )
    return []
