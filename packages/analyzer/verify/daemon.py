"""verify.daemon — The verify daemon entry point.

Spec: docs/archive/specs/spec-rsi-layer-2-verify-and-sysadmin-2026-04-18.md §4.

Runs every 5 minutes via launchd (pure logic, no LLM). Each cycle:

  1. Scan ``{shared_dir}/proposals/applied/`` for proposals whose
     ``apply_time + claim.window_days <= now``.
  2. For each expired proposal:
       - Resolve the metric via the metric registry.
       - Evaluate the claim.
       - Dispatch (succeeded / failed_reverted / failed_flagged /
         failed_revert_failed / unresolved-retry).
       - Write a verification-result JSON.
       - Persist the proposal's new state back to disk (moving it
         between status directories).
       - If an escalation was synthesized, write it to
         ``proposals/pending/``.

Metric-resolution failures bump an attempt counter on the proposal. After
3 consecutive failures, the proposal is force-flagged and an escalation
proposal is emitted.

The daemon does not require a persistent process — each cycle is
idempotent. Run via ``python -m verify.daemon``.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

from evolve_util import atomic_write_json as _atomic_write_json
from evolve_util import now_iso_offset as _utc_now_iso
from schema.proposal import Proposal
from verify.dispatch import dispatch_outcome
from verify.evaluate import evaluate_claim
from verify.verification_result import VerificationResult, write_result

# Tool-suspend guard — see SKILL spec §5.5.5c. If verify.py's meta-test
# (M3 in tool-meta-tests catalog) detects that verify mis-evaluates a
# claim that should always pass, the worker writes a suspend flag and
# verify must stop acting (otherwise it could mark good proposals as
# failed_verify, triggering needless reverts).
try:
    from evolve_admin.tool_suspend import guard as _suspend_guard
except ImportError:
    def _suspend_guard(_tool: str, log_fn=None, repo_root=None) -> bool:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

MAX_RESOLUTION_RETRIES = 3


def _parse_iso(raw: str) -> datetime | None:
    candidate = raw
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ─────────────────────────────────────────────────────────────────────────────
# Metric resolver callable type
# ─────────────────────────────────────────────────────────────────────────────

# The daemon depends on the metric registry indirectly: callers provide a
# resolver callable matching this signature. In production the callable is
# ``metrics.registry.resolve``. In tests, it's a mock.

MetricResolveFn = Callable[[str, str, datetime], "ResolveResult"]


@dataclass
class ResolveResult:
    """What the metric resolver returns."""

    value: float
    confidence: float = 1.0
    source_note: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Daemon
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class VerifyCycleReport:
    """Summary of what one run of the daemon did."""

    scanned: int = 0
    succeeded: int = 0
    failed_reverted: int = 0
    failed_flagged: int = 0
    failed_revert_failed: int = 0
    unresolved: int = 0
    retry_exhausted: int = 0
    escalations_emitted: int = 0
    not_expired: int = 0
    malformed: list[tuple[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "succeeded": self.succeeded,
            "failed_reverted": self.failed_reverted,
            "failed_flagged": self.failed_flagged,
            "failed_revert_failed": self.failed_revert_failed,
            "unresolved": self.unresolved,
            "retry_exhausted": self.retry_exhausted,
            "escalations_emitted": self.escalations_emitted,
            "not_expired": self.not_expired,
            "malformed_count": len(self.malformed),
            "malformed": self.malformed,
        }


class VerifyDaemon:
    """A single-cycle verify daemon runner.

    Construction takes a ``resolve_fn`` callable that the daemon uses to
    resolve metrics. In production this is ``metrics.registry.resolve``;
    in tests, a mock.
    """

    def __init__(
        self,
        *,
        shared_dir: Path,
        resolve_fn: MetricResolveFn,
        now: datetime | None = None,
    ) -> None:
        self.shared_dir = Path(shared_dir)
        self.resolve_fn = resolve_fn
        self._now = now  # None → use real clock

    # ── Helpers ─────────────────────────────────────────────────────────────

    @property
    def applied_dir(self) -> Path:
        return self.shared_dir / "proposals" / "applied"

    @property
    def pending_dir(self) -> Path:
        return self.shared_dir / "proposals" / "pending"

    @property
    def succeeded_dir(self) -> Path:
        return self.shared_dir / "proposals" / "succeeded"

    @property
    def failed_reverted_dir(self) -> Path:
        return self.shared_dir / "proposals" / "failed_reverted"

    @property
    def failed_flagged_dir(self) -> Path:
        return self.shared_dir / "proposals" / "failed_flagged"

    @property
    def failed_revert_failed_dir(self) -> Path:
        return self.shared_dir / "proposals" / "failed_revert_failed"

    def _now_dt(self) -> datetime:
        if self._now is not None:
            return self._now
        return datetime.now(timezone.utc)

    def _apply_time(self, proposal: Proposal) -> datetime | None:
        """Find the apply_time from history (the entry transitioning to 'applied')."""
        for entry in proposal.history:
            if entry.to_status == "applied":
                return _parse_iso(entry.at)
        return None

    def _claim_horizon(self, proposal: Proposal) -> datetime | None:
        apply_time = self._apply_time(proposal)
        if apply_time is None or proposal.claim is None:
            return None
        return apply_time + timedelta(days=proposal.claim.window_days)

    def _attempt_count(self, proposal: Proposal) -> int:
        return int(proposal.provenance.signals.get("_verify_attempts", 0))

    def _bump_attempts(self, proposal: Proposal) -> int:
        new_count = self._attempt_count(proposal) + 1
        proposal.provenance.signals["_verify_attempts"] = new_count
        return new_count

    # ── File movement ──────────────────────────────────────────────────────

    def _target_dir_for_status(self, status: str) -> Path:
        return {
            "succeeded": self.succeeded_dir,
            "failed_reverted": self.failed_reverted_dir,
            "failed_flagged": self.failed_flagged_dir,
            "failed_revert_failed": self.failed_revert_failed_dir,
        }[status]

    def _move_proposal(self, proposal: Proposal, current_path: Path) -> Path:
        """Move the proposal file from its current location to its status dir.

        Holds the proposal-store lock and verifies the source still
        exists under it (7.1 Phase A) — without that, a verify-vs-
        "mark complete" race re-creates an already-moved file in a
        second location. The verify taxonomy (succeeded/, failed_*)
        is private to this daemon, so arbiter.store.move_proposal's
        Subdir typing doesn't fit; the lock + source check give the
        same race protection.
        """
        from arbiter.store import locked as _proposals_locked
        with _proposals_locked(self.shared_dir):
            if not current_path.exists():
                raise OSError(
                    f"verify._move_proposal: source vanished (lost a "
                    f"concurrent transition race?): {current_path}"
                )
            target_dir = self._target_dir_for_status(proposal.status)
            target_dir.mkdir(parents=True, exist_ok=True)
            target_path = target_dir / current_path.name
            # Write new content, then delete old location
            _atomic_write_json(target_path, proposal.to_dict(), sort_keys=True)
            try:
                current_path.unlink()
            except OSError:
                pass
            return target_path

    def _update_in_place(self, proposal: Proposal, path: Path) -> None:
        """Write the proposal back to its current file (retry case)."""
        _atomic_write_json(path, proposal.to_dict(), sort_keys=True)

    # ── Applied-proposals scanner ──────────────────────────────────────────

    def _iter_applied(self) -> Iterator[tuple[Path, Proposal]]:
        if not self.applied_dir.exists():
            return
        for path in sorted(self.applied_dir.glob("*.json")):
            try:
                with path.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            try:
                proposal = Proposal.from_dict(data)
            except (KeyError, ValueError, TypeError):
                continue
            yield path, proposal

    # ── Main cycle ──────────────────────────────────────────────────────────

    def run_cycle(self) -> VerifyCycleReport:
        """Run one verification cycle over the applied proposals."""
        report = VerifyCycleReport()
        now = self._now_dt()

        # Pre-build the registry once per cycle so per-proposal track_record
        # bumps don't re-walk the generators_code_dir. Falls back to
        # per-call construction inside the helper if pre-build fails.
        try:
            from arbiter.track_record import build_registry_for_bumps

            self._tr_registry = build_registry_for_bumps(self.shared_dir)
        except Exception:
            self._tr_registry = None

        for path, proposal in self._iter_applied():
            report.scanned += 1

            if proposal.claim is None:
                # Manual-completion kinds (Investigation, WorkflowInstruction)
                # legitimately sit in applied/ awaiting an operator click on
                # "Mark complete." Don't rescue them — they're the In
                # Process queue's contents.
                from arbiter.apply import is_manual_completion_kind

                action_kind = getattr(
                    proposal.action,
                    "kind",
                    type(proposal.action).__name__,
                )
                if is_manual_completion_kind(action_kind):
                    report.not_expired += 1
                    continue

                # Auto-applied claim-less proposals from before apply.py
                # learned to promote them. Rescue by transitioning to
                # ``succeeded`` and moving the file out of applied/.
                from arbiter.state_machine import (
                    IllegalTransitionError as _IllegalTransitionError,
                    transition as _transition,
                )

                try:
                    _transition(
                        proposal,
                        "succeeded",
                        actor="verify_daemon",
                        reason="no claim to verify; closed by daemon sweep",
                    )
                except _IllegalTransitionError:
                    report.malformed.append(
                        (path.name, "no claim on applied proposal")
                    )
                    continue
                self._move_proposal(proposal, path)
                self._bump_track_record(proposal, "succeeded")
                report.succeeded += 1
                continue

            horizon = self._claim_horizon(proposal)
            if horizon is None:
                report.malformed.append(
                    (path.name, "cannot determine claim horizon")
                )
                continue

            if horizon > now:
                report.not_expired += 1
                continue

            self._process_expired(proposal, path, now, report)

        return report

    def _process_expired(
        self,
        proposal: Proposal,
        path: Path,
        now: datetime,
        report: VerifyCycleReport,
    ) -> None:
        assert proposal.claim is not None  # checked by caller

        # Resolve metric
        try:
            resolve = self.resolve_fn(proposal.claim.metric, proposal.bot_id, now)
        except Exception as e:  # noqa: BLE001 — resolver errors are expected
            self._handle_resolution_error(
                proposal, path, now, report, error_message=str(e)
            )
            return

        # Evaluate claim
        outcome = evaluate_claim(
            proposal.claim,
            current_value=resolve.value,
            current_confidence=resolve.confidence,
        )

        if outcome.result == "unresolved":
            self._handle_resolution_error(
                proposal,
                path,
                now,
                report,
                error_message=outcome.message,
                current_value=resolve.value,
                current_confidence=resolve.confidence,
            )
            return

        # Dispatch (mutates proposal.status)
        dispatch = dispatch_outcome(proposal, outcome)

        # Write verification result
        vr = VerificationResult(
            proposal_id=proposal.id,
            bot_id=proposal.bot_id,
            verified_at=_utc_now_iso(),
            metric=proposal.claim.metric,
            baseline=proposal.claim.baseline,
            current_value=resolve.value,
            current_confidence=resolve.confidence,
            delta=outcome.delta,
            direction=proposal.claim.direction,
            magnitude=proposal.claim.magnitude,
            window_days=proposal.claim.window_days,
            fallback=proposal.claim.fallback,
            outcome=proposal.status,
            revert_ok=dispatch.revert_ok,
            revert_message=dispatch.revert_message,
            notes=dispatch.notes,
        )
        write_result(vr, self.shared_dir)

        # Move proposal to its new status dir
        self._move_proposal(proposal, path)

        # Write escalation proposal, if any
        if dispatch.escalation is not None:
            # Escalation starts in draft; write directly to pending dir
            # Mimicking what the ingest pipeline would do. In L1 terms,
            # daemons write to pending/ without running ingest because they
            # trust their own construction.
            from arbiter.state_machine import transition

            transition(
                dispatch.escalation,
                "pending",
                actor="verify_daemon",
                reason="auto-emitted by verify daemon",
            )
            self.pending_dir.mkdir(parents=True, exist_ok=True)
            esc_path = self.pending_dir / f"{dispatch.escalation.id}.json"
            _atomic_write_json(esc_path, dispatch.escalation.to_dict(), sort_keys=True)
            report.escalations_emitted += 1

        # Update report counters
        if proposal.status == "succeeded":
            report.succeeded += 1
        elif proposal.status == "failed_reverted":
            report.failed_reverted += 1
        elif proposal.status == "failed_flagged":
            report.failed_flagged += 1
        elif proposal.status == "failed_revert_failed":
            report.failed_revert_failed += 1

        # Update the generator's TrackRecord. The verify daemon is the
        # canonical place for verified-success / verified-failed bumps —
        # claim-ful proposals only reach a terminal status here.
        self._bump_track_record(proposal, proposal.status)

    def _handle_resolution_error(
        self,
        proposal: Proposal,
        path: Path,
        now: datetime,
        report: VerifyCycleReport,
        *,
        error_message: str,
        current_value: float = 0.0,
        current_confidence: float = 0.0,
    ) -> None:
        """Retry logic: bump attempts, force-flag after MAX_RESOLUTION_RETRIES."""
        attempts = self._bump_attempts(proposal)

        if attempts < MAX_RESOLUTION_RETRIES:
            # Leave in applied/; persist bumped attempt count.
            self._update_in_place(proposal, path)
            report.unresolved += 1
            return

        # Exhausted retries — force-flag
        from arbiter.state_machine import transition

        transition(
            proposal,
            "failed_flagged",
            actor="verify_daemon",
            reason=(
                f"metric resolver failed {attempts} times: {error_message}"
            ),
        )
        # Emit escalation
        from schema.proposal import (
            Investigation,
            Proposal as _P,
            Provenance,
            RiskTag,
            new_proposal_id,
        )

        assert proposal.claim is not None
        escalation = _P(
            id=new_proposal_id(),
            bot_id=proposal.bot_id,
            generator_id="verify_daemon",
            dimension="meta_health",
            trigger_observations=[f"resolution_failed:{proposal.id}"],
            provenance=Provenance(
                technique="verify_daemon.resolution_exhausted",
                signals={
                    "original_proposal_id": proposal.id,
                    "attempts": attempts,
                    "metric": proposal.claim.metric,
                },
                confidence=0.99,
            ),
            problem=(
                f"Cannot verify {proposal.id}: metric "
                f"{proposal.claim.metric!r} failed to resolve after "
                f"{attempts} attempts: {error_message}"
            ),
            action=Investigation(
                context=f"Resolver repeatedly failed. Last error: {error_message}"
            ),
            risk_tag=RiskTag(blast_radius="bot", reversibility="manual", touches=[]),
            approval_audience="pod_operator",
            urgency="operational_urgent",
            admin_surface_summary=f"Unverifiable proposal {proposal.id[:8]}: resolver failed"[
                :120
            ],
            status="draft",
        )
        transition(
            escalation,
            "pending",
            actor="verify_daemon",
            reason="auto-emitted by verify daemon",
        )

        # Write verification result for audit
        vr = VerificationResult(
            proposal_id=proposal.id,
            bot_id=proposal.bot_id,
            verified_at=_utc_now_iso(),
            metric=proposal.claim.metric,
            baseline=proposal.claim.baseline,
            current_value=current_value,
            current_confidence=current_confidence,
            delta=0.0,
            direction=proposal.claim.direction,
            magnitude=proposal.claim.magnitude,
            window_days=proposal.claim.window_days,
            fallback=proposal.claim.fallback,
            outcome="failed_flagged",
            notes=[
                f"resolver failed {attempts} times: {error_message}",
                "forced to failed_flagged after retry exhaustion",
            ],
        )
        write_result(vr, self.shared_dir)

        self._move_proposal(proposal, path)
        self._bump_track_record(proposal, "failed_flagged")
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        esc_path = self.pending_dir / f"{escalation.id}.json"
        _atomic_write_json(esc_path, escalation.to_dict(), sort_keys=True)

        report.retry_exhausted += 1
        report.failed_flagged += 1
        report.escalations_emitted += 1

    def _bump_track_record(self, proposal: Proposal, to_status: str) -> None:
        """Best-effort TrackRecord update for a terminal transition the
        daemon just made. Logs and continues on any failure — bookkeeping
        must never block the verify lifecycle, but silent failures hide
        regressions, so we log at WARNING so an operator tail-following
        the daemon log notices.

        Reuses ``self._tr_registry`` (built once per cycle in
        :meth:`run_cycle`) so per-proposal bumps don't re-walk the
        generator records dir.
        """
        try:
            from arbiter.track_record import bump_for_status_transition

            bump_for_status_transition(
                self.shared_dir,
                proposal,
                to_status=to_status,
                registry=getattr(self, "_tr_registry", None),
            )
        except Exception as exc:  # noqa: BLE001
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "verify_daemon: track_record bump failed for %s (status=%s): %s",
                proposal.id,
                to_status,
                exc,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Convenience run-once
# ─────────────────────────────────────────────────────────────────────────────


def run_once(
    shared_dir: Path,
    resolve_fn: MetricResolveFn,
    *,
    now: datetime | None = None,
) -> VerifyCycleReport:
    """Run a single verify cycle. Used by the LaunchDaemon and by tests."""
    # Tool-suspend guard — runs first. If the M3 meta-test caught
    # verify making wrong decisions (claim that should pass evaluating
    # as failed_verify), the worker has set the suspend flag. Skipping
    # the cycle prevents needless reverts of good proposals. Returns
    # an empty cycle report — looks like a tick with nothing to verify.
    if _suspend_guard("verify", log_fn=lambda m: print(f"[evolve/verify] {m}")):
        # Default-constructed report = all counters 0; looks like a tick
        # with nothing to verify (the truth: we deliberately skipped).
        return VerifyCycleReport()
    daemon = VerifyDaemon(shared_dir=shared_dir, resolve_fn=resolve_fn, now=now)
    return daemon.run_cycle()


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one cycle of the verify daemon."
    )
    parser.add_argument(
        "--shared-dir",
        type=Path,
        default=Path("/Users/Shared/evolve"),
        help="Evolve shared directory.",
    )
    args = parser.parse_args(argv)

    # Import here to keep the CLI independent of metric-resolver availability
    # during testing
    from metrics.registry import resolve as metrics_resolve  # noqa: E402

    def resolver(metric: str, bot_id: str, as_of: datetime) -> ResolveResult:
        value = metrics_resolve(metric, bot_id, as_of)
        return ResolveResult(
            value=value.value,
            confidence=value.confidence,
            source_note=value.source_note,
        )

    report = run_once(args.shared_dir, resolver)
    print(json.dumps(report.as_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
