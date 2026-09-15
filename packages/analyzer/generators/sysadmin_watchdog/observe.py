"""generators.sysadmin_watchdog.observe — Entry points.

Two surfaces, mirroring the alerts/signal-store split:

  * :func:`observe_signals` — runs every platform detector's signal-emitting
    pass and returns Signal specs. Called first by the generator runner so
    that the Signal store is current before any proposal lookups.

  * :func:`observe` — runs proposal-emitting passes (today: only ACL drift)
    and returns Proposals. ACL's ``detect()`` looks up the matching active
    Signal via ``ctx.shared_dir`` and back-fills ``motivating_signals[]``.

Each detector is a pure function: signal detectors return ``dict | None``,
proposal detectors return ``list[Proposal]``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from metrics import MetricValue, resolve as metrics_resolve
from proposal_synthesizer.emit import emit_from_proposal
from schema.candidate_proposal import Magnitude
from schema.proposal import Proposal


# ─────────────────────────────────────────────────────────────────────────────
# DetectorContext
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class DetectorContext:
    """Shared context passed to every detector.

    Fields:
        bot_id: the bot being observed.
        now: "current" timestamp; tests override for determinism.
        resolve: metric-resolution function (defaults to metrics.resolve).
            Tests inject a mock.
        config: generator's evolvable configuration dict (defaults {}).
            Detectors read their tunable thresholds from here.
        sysadmin_audience: the bot's resolved sysadmin audience, for routing.
        shared_dir: pod shared dir; needed by detectors that link proposals
            back to motivating Signals. ``None`` in unit tests that don't
            exercise the cross-link.
    """

    bot_id: str
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    resolve: Callable[[str, str, datetime], MetricValue] = field(
        default=metrics_resolve
    )
    config: dict = field(default_factory=dict)
    sysadmin_audience: str = "pod_operator"
    shared_dir: Path | None = None

    def metric(self, name: str) -> MetricValue:
        """Resolve a metric for this context's bot at the current time."""
        return self.resolve(name, self.bot_id, self.now)


# ─────────────────────────────────────────────────────────────────────────────
# Observe — signals
# ─────────────────────────────────────────────────────────────────────────────


def observe_signals(ctx: DetectorContext) -> list[dict]:
    """Run every signal-emitting platform detector and return Signal specs.

    The generator runner walks this list and writes each spec via
    ``signals.store.observe(**spec)`` *before* calling :func:`observe`,
    so any proposal that needs to back-link can read the resulting Signal
    by signature.
    """
    from generators.sysadmin_watchdog.detectors.platform import (
        ALL_SIGNAL_DETECTORS,
    )

    specs: list[dict] = []
    for detector in ALL_SIGNAL_DETECTORS:
        try:
            spec = detector(ctx)
        except Exception as e:  # noqa: BLE001 — detector bugs shouldn't crash observe
            import sys

            print(
                f"sysadmin_watchdog: signal detector {detector.__module__} raised: {e}",
                file=sys.stderr,
            )
            continue
        if spec:
            specs.append(spec)
    return specs


# ─────────────────────────────────────────────────────────────────────────────
# Observe — proposals
# ─────────────────────────────────────────────────────────────────────────────


def observe(ctx: DetectorContext) -> list[Proposal]:
    """Run all proposal-emitting platform detectors and return Proposals.

    Today only ACL drift produces a Proposal (an autonomous-eligible
    ConfigPatch). Every other platform-failure detector routes purely
    through :func:`observe_signals`.
    """
    from generators.sysadmin_watchdog.detectors.platform import ALL_DETECTORS

    proposals: list[Proposal] = []
    for detector in ALL_DETECTORS:
        try:
            emitted = detector(ctx)
        except Exception as e:  # noqa: BLE001
            import sys

            print(
                f"sysadmin_watchdog: detector {detector.__module__} raised: {e}",
                file=sys.stderr,
            )
            continue
        if emitted:
            proposals.extend(emitted)

    # Phase A.5 — filter dismissed signatures before emission.
    from arbiter.dismissals import filter_dismissed
    proposals = filter_dismissed(proposals, ctx.shared_dir, ctx.bot_id)

    # Phase 6c cutover: emit each Proposal as a CandidateProposal and
    # return []. The substantiveness gate promotes promotables.
    for p in proposals:
        tech = (p.provenance.technique or "").split(".", 1)[-1]
        emit_from_proposal(
            p,
            variant=tech or "platform_finding",
            magnitude=Magnitude(unit="count", value=1.0),
            shared_dir=ctx.shared_dir,
        )

    return []


