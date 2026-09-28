"""Platform detector: ACL — evolve user read access on .openclaw/ dir.

ACL drift is the one substrate-health condition that emits *both* a
Signal and a Proposal. The Proposal is autonomous-eligible (a
ConfigPatch the applier can run unattended); the Signal exists so the
operator can see "evolve can't read /Users/<bot>/.openclaw" on the
Alerts page even before the proposal applies, and so the bidirectional
``motivating_signals`` link required by Phase 4 is populated.
"""

from __future__ import annotations

from generators.sysadmin_watchdog import pitches
from generators.sysadmin_watchdog.observe import DetectorContext
from generators.sysadmin_watchdog.proposals import make_acl_drift_fix
from generators.sysadmin_watchdog.signals import (
    PRODUCER,
    acl_drift_signal_kwargs,
)
from schema.proposal import Proposal
from schema.signal import make_signature


def _withhold_during_bringup(ctx: DetectorContext) -> bool:
    """Fresh-pod settle gate: the deploy re-asserts evolve's read ACL during
    bring-up (``heal_evolve_access`` / ``_final_evolve_access_pass``), so a
    transient ``acl.evolve_read`` miss on a not-yet-settled pod is self-healing
    noise, not drift. acl_drift is severity ``warn`` (non-critical), so it is
    withheld until the pod settles. See
    internal/spec-pod-bringup-settle-2026-06-23.md."""
    if ctx.shared_dir is None:
        return False
    try:
        from signals.settle_gate import should_withhold
    except ImportError:
        return False
    return should_withhold(ctx.shared_dir, severity="warn", transient=True)


def detect_signal(ctx: DetectorContext) -> dict | None:
    acl = ctx.metric("acl.evolve_read")
    signature = make_signature(PRODUCER, "acl_drift", ctx.bot_id)
    if acl.value >= 1.0:
        # ACL is healthy this cycle — reset any in-flight dwell so a later
        # re-clamp starts counting from zero (single-cycle flaps never page).
        if ctx.shared_dir is not None:
            from signals import flap_gate

            flap_gate.note_cleared(ctx.shared_dir, signature)
        return None
    if _withhold_during_bringup(ctx):
        return None
    # Self-healing flap hysteresis: the evolve-read ACL is re-clamped on every
    # gateway re-harden and auto-restored seconds later, so a one-cycle miss is
    # noise. Require the miss to persist across N consecutive watchdog cycles
    # before paging. ctx.shared_dir is None only in unit tests that don't
    # exercise the store — there we page immediately (no behavior change).
    # See internal/spec-transient-signal-suppression-2026-06-23.md.
    if ctx.shared_dir is not None:
        from signals import flap_gate

        verdict = flap_gate.note_observed(
            ctx.shared_dir, signature=signature, transient=True, type="acl_drift"
        )
        if not verdict.page:
            return None
    return acl_drift_signal_kwargs(ctx.bot_id)


def detect(ctx: DetectorContext) -> list[Proposal]:
    acl = ctx.metric("acl.evolve_read")
    if acl.value >= 1.0:
        return []
    if _withhold_during_bringup(ctx):
        return []

    # Fire the autonomous ACL-restore fix in lock-step with its Signal: while
    # the miss is still dwelling (the Signal hasn't been emitted this cycle by
    # observe_signals, which the runner always runs first), withhold the
    # proposal too. Once the Signal is firing, the fix flows normally.
    signature = make_signature(PRODUCER, "acl_drift", ctx.bot_id)
    if ctx.shared_dir is not None:
        from signals import flap_gate

        if not flap_gate.signal_is_active(ctx.shared_dir, signature):
            return []

    acl_config_path = ctx.config.get(
        "acl_config_path",
        f"/Users/Shared/evolve/acl/{ctx.bot_id}.json::acl_restored",
    )
    summary = pitches.ACL_DRIFT_SUMMARY.format(bot_id=ctx.bot_id)

    proposal = make_acl_drift_fix(
        ctx.bot_id,
        acl_config_path=acl_config_path,
        admin_summary=summary,
        conversational_pitch=(
            pitches.ACL_DRIFT_CONV
            if ctx.sysadmin_audience in ("primary_user", "both")
            else None
        ),
    )

    # Back-link to the motivating Signal (written earlier this cycle by
    # observe_signals()). Best-effort — if the lookup fails (e.g. tests
    # without a shared_dir, or the signals layer is unreachable) the
    # proposal still fires; only the link is missing.
    if ctx.shared_dir is not None:
        try:
            from signals import store as signals_store

            existing = signals_store.find_active_by_signature(
                ctx.shared_dir, signature
            )
            if existing is not None and existing.id not in proposal.motivating_signals:
                proposal.motivating_signals.append(existing.id)
        except Exception:
            pass

    return [proposal]
