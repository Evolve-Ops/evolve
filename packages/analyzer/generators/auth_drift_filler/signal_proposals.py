"""generators.auth_drift_filler.signal_proposals — Signal → Proposal factory.

``make_drift_proposals`` takes one ``perm_config_drift`` Signal and emits a
*list* of UpdatePermissionConfig Proposals — one per drifted field.
Per-field proposals let the operator selectively accept / reject; a
bundled "restore all drifted fields" proposal would force all-or-nothing.

The signal's ``details.diffs`` already carries
``{dotted_field: {"expected": ..., "observed": ...}}``, so this
generator never has to re-read openclaw.json. The expected value
straight from the signal becomes the new field value.

Investigate-before-propose (spec internal/spec-config-intent-system-2026-05-21.md
§4): before emitting a revert proposal for a drifted field, check the
config-intent sidecar. If an active intent records that the deviation is
deliberate, suppress the proposal and (once per intent, via the generator
dedup log) emit a low-severity audit-only Signal so the trade-off is
visible without being naggy. This stops the recurring noise reported on
2026-05-24 (six restore-to-deny proposals every sweep, forever).

Action kind note: emits ``UpdatePermissionConfig`` (not ``ConfigPatch``)
so the apply path routes through ``UpdatePermissionConfigApplier`` →
``permissions.writer.write_openclaw_fields``, the canonical /tmp + sudo
helper. ConfigPatch's generic L1 patcher uses
``tempfile.mkstemp(dir=path.parent)`` which fails on member bots whose
``/Users/<bot>/.openclaw/`` is bot-owned with evolve having only a
read-only ACL. See internal/diagnosis-openclaw-json-write-regression-2026-05-21.md.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from schema.proposal import (
    Proposal,
    Provenance,
    RiskTag,
    UpdatePermissionConfig,
    new_proposal_id,
)

from evolve_config import bot_label


GENERATOR_ID = "auth_drift_filler"
DIMENSION = "safety"


# ── Dismiss signatures (Phase A.5 + Phase C-6) ──────────────────────────────
#
# Per-field granularity: each drifted permission field gets its own
# signature. Dismissing one field's revert proposal (operator decides
# the drift is intentional but doesn't want to record a formal
# config_intent) doesn't suppress unrelated drifted fields.
def dismiss_signature_for_field(field: str) -> str:
    return f"{GENERATOR_ID}:perm_config_drift:{field}"

# Producer name used when this generator emits its own audit-only Signals
# (deliberate-deviation visibility). Distinct from ``permission_monitor``
# (the source of perm_config_drift Signals this generator consumes) so the
# Phase 4 UI can filter cleanly: "what intentional deviations are recorded
# vs. what actual drift is open".
AUDIT_SIGNAL_PRODUCER = "auth_drift_filler"
AUDIT_SIGNAL_TYPE = "perm_config_intent_audit"


def _signal_dict_get(signal: Any, key: str, default: Any = None) -> Any:
    """Read from a Signal dataclass or a plain dict — useful for tests."""
    if isinstance(signal, dict):
        return signal.get(key, default)
    return getattr(signal, key, default)


def _format_value(v: Any) -> str:
    """Format a JSON-ish scalar/list/dict for inclusion in problem prose.

    The drift values come from openclaw.json so they're already JSON-
    serializable; ``repr`` is good enough for the operator-facing
    summary and renders booleans / None readably.
    """
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    return repr(v)


def _get_intent_safely(bot_id: str, field: str,
                        shared_dir: Path | None) -> dict | None:
    """Look up an intent record without letting import or read failures
    poison the generator loop.

    Fail-open behavior: a missing import or unreadable sidecar returns
    None (i.e. "no intent recorded"), so the generator falls back to
    the legacy revert-proposal path. The safe direction — over-
    recommending beats under-protecting. See the equivalent guard in
    ``evolve_admin.config_intent.get_intent`` itself.
    """
    try:
        from evolve_admin.config_intent import get_intent
    except ImportError:
        return None
    try:
        return get_intent(bot_id, field, shared_dir=shared_dir)
    except Exception:  # noqa: BLE001 — generator-loop hygiene
        return None


def _maybe_emit_audit_signal(
    *, bot_id: str, field: str, intent: dict,
    observed: Any, expected: Any,
    sig_id: str, shared_dir: Path | None,
) -> None:
    """Emit a once-per-intent audit-only Signal so the deliberate deviation
    is visible somewhere (Phase 4 surfaces these in Security → Intentional
    Deviations). Dedups via the generator memory log so subsequent sweeps
    are quiet — visibility is good, pestering is not.

    No-ops cleanly on any failure (missing shared_dir, signal-store import
    error, write error). The audit-only signal is informational; failure
    must not block the surrounding suppression behavior.
    """
    if shared_dir is None:
        return
    try:
        from generators._intent_memory import already_emitted, record_emission
        from signals import store as signals_store
        from schema.signal import make_signature
    except ImportError:
        return

    intent_id = intent.get("id", "<no-id>")
    dedup_signature = f"{bot_id}:{field}:audit_only:{intent_id}"
    try:
        if already_emitted(
            generator_id=GENERATOR_ID,
            signature=dedup_signature,
            shared_dir=shared_dir,
        ):
            return
    except Exception:  # noqa: BLE001
        return

    title = (
        f"{bot_id}: {field}={_format_value(observed)} is a recorded "
        f"intentional deviation"
    )
    body = (
        f"Intent {intent_id} recorded {intent.get('set_at', '')} by "
        f"{intent.get('set_by', '')}. Reason: {intent.get('reason', '')}. "
        f"Baseline would restore to {_format_value(expected)}. Revoke the "
        f"intent (Security → Intentional Deviations) to surface drift "
        f"proposals for this field again."
    )
    try:
        signals_store.observe(
            shared_dir,
            signature=make_signature(AUDIT_SIGNAL_PRODUCER,
                                     AUDIT_SIGNAL_TYPE,
                                     f"{bot_id}:{field}:{intent_id}"),
            producer=AUDIT_SIGNAL_PRODUCER,
            type=AUDIT_SIGNAL_TYPE,
            flavor="security",
            severity="info",
            scope="bot",
            bot_id=bot_id,
            title=title,
            body=body,
            details={
                "bot_id": bot_id,
                "field": field,
                "observed": observed,
                "baseline": expected,
                "intent_id": intent_id,
                "intent_reason": intent.get("reason"),
                "intent_set_by": intent.get("set_by"),
                "motivating_drift_signal_id": sig_id,
            },
            caused_by_signal_id=sig_id or None,
        )
    except Exception:  # noqa: BLE001
        # Don't record emission if observe() failed — let the next sweep
        # retry. record_emission failure below is fine: at worst the
        # next sweep emits one duplicate, which observe() then dedups
        # via signature anyway.
        return
    try:
        record_emission(
            generator_id=GENERATOR_ID,
            signature=dedup_signature,
            shared_dir=shared_dir,
        )
    except Exception:  # noqa: BLE001
        return


def make_drift_proposals(signal: Any,
                         *,
                         shared_dir: Path | None = None) -> list[Proposal]:
    """Turn one ``perm_config_drift`` Signal into N per-field proposals.

    Drops fields where ``expected == observed`` defensively even though
    the monitor only reports diffs — protects against a future change
    that loosens the producer's filter.

    Skips fields whose expected value is ``None`` (no baseline opinion
    set; nothing meaningful to restore). The operator can still
    handle these manually if needed.

    Skips fields whose deviation matches an active config-intent record
    (spec internal/spec-config-intent-system-2026-05-21.md §4). For each
    suppressed field, emits a once-per-intent audit-only Signal so the
    deliberate deviation has a place to surface (Phase 4 UI). The
    ``shared_dir`` kwarg threads through from observe(); the legacy
    call form (positional signal only) keeps the old behavior so
    existing unit tests stay green.
    """
    bot_id = _signal_dict_get(signal, "bot_id") or "<unknown>"
    sig_id = _signal_dict_get(signal, "id") or ""
    details = _signal_dict_get(signal, "details")
    if not isinstance(details, dict):
        return []
    diffs = details.get("diffs")
    if not isinstance(diffs, dict) or not diffs:
        return []

    # Lazy import — fail-open if config_intent module isn't present
    # (e.g. mid-rollout, or test env that bypasses evolve_admin).
    try:
        from evolve_admin.config_intent import intent_still_valid
    except ImportError:
        intent_still_valid = None  # type: ignore[assignment]

    proposals: list[Proposal] = []

    for field, diff in diffs.items():
        if not isinstance(diff, dict):
            continue
        expected = diff.get("expected")
        observed = diff.get("observed")
        if expected == observed:
            # Defensive: producer shouldn't include identical pairs, but
            # if a future iteration does, skip rather than emit a
            # no-op proposal.
            continue
        if expected is None:
            # No baseline value to restore — operator handles manually.
            continue
        if not isinstance(field, str) or not field:
            continue

        # Investigate before proposing: is this deviation recorded as
        # deliberate? Only check when shared_dir is supplied (observe()
        # always supplies it; direct factory tests may not).
        intent = (
            _get_intent_safely(bot_id, field, shared_dir)
            if shared_dir is not None else None
        )
        if intent is not None and intent.get("value") == observed:
            # The intent's recorded value matches what's actually on disk,
            # so the deviation is intentional. Suppress the revert
            # proposal; surface the trade-off once via an audit signal.
            still_valid = (
                intent_still_valid(intent) if intent_still_valid else True
            )
            if still_valid:
                _maybe_emit_audit_signal(
                    bot_id=bot_id, field=field, intent=intent,
                    observed=observed, expected=expected,
                    sig_id=sig_id, shared_dir=shared_dir,
                )
                continue
            # Intent exists but is stale (Phase 5 plugin-coupled check).
            # Phase 1's intent_still_valid always returns True, so this
            # branch is currently unreachable; left here so the Phase 5
            # wiring lands as a one-line behavioral flip without
            # reshaping the loop.

        bot_name = bot_label(bot_id)
        problem = (
            f"{bot_name}: permission field {field!r} drifted "
            f"(observed={_format_value(observed)}, "
            f"baseline={_format_value(expected)}). "
            f"Proposal restores baseline."
        )
        # Phase C-6 humanized title — drops the slug + value pair.
        # Field name still appears (the operator needs to know which
        # field) but the raw value goes to Details.
        headline = f"Restore {bot_name}'s {field} setting to the baseline"

        # ── Phase C-6 operator-first content (Tier 1 — auto-apply) ──────
        summary = (
            f"{bot_name}'s permission setting {field!r} drifted from the "
            f"pod-wide baseline. The current value is "
            f"{_format_value(observed)}; the baseline is "
            f"{_format_value(expected)}. Applying restores the baseline "
            f"value; the prior value is captured for revert."
        )
        explanation = (
            f"Each bot's permission config (`openclaw.json`) tracks "
            f"a pod-wide baseline so the engine can spot drift — a "
            f"value that quietly diverged because of a manual edit, "
            f"a deploy hiccup, or an experiment someone forgot to "
            f"clean up.\n\n"
            f"Diagnosis. The monitor saw {field!r} as "
            f"{_format_value(observed)} on disk but the baseline says "
            f"{_format_value(expected)}. We don't know whether the "
            f"deviation was deliberate; the safe direction is to "
            f"propose restoring the baseline and let you confirm.\n\n"
            f"What to do. If the baseline value is the one you want, "
            f"approve this and the engine writes it back. If the "
            f"current value is intentional (you tightened or "
            f"loosened a permission deliberately), dismiss this and "
            f"record a config-intent so the engine learns the choice "
            f"is permanent — future drift sweeps will see the intent "
            f"and skip this field entirely.\n\n"
            f"What could go wrong. Reverting a deliberate tighten "
            f"weakens the bot's security back to a looser default; "
            f"reverting a deliberate loosen breaks whatever you were "
            f"trying to enable. The config-intent system is the "
            f"durable answer for both — see Permissions → Settings "
            f"for the recording UI."
        )

        proposals.append(Proposal(
            id=new_proposal_id(),
            bot_id=bot_id,
            generator_id=GENERATOR_ID,
            dimension=DIMENSION,
            trigger_observations=[f"perm_config_drift:{bot_id}:{field}"],
            provenance=Provenance(
                technique="auth_drift_filler.config_drift",
                signals={
                    "bot_id": bot_id,
                    "field": field,
                    "expected": expected,
                    "observed": observed,
                },
                confidence=0.95,
            ),
            problem=problem,
            action=UpdatePermissionConfig(
                bot_id=bot_id,
                # deepcopy the baseline value so downstream applier code
                # mutating the dict/list shape can't leak back into the
                # signal store's in-memory snapshot. (cron_caps_filler
                # does the same; parity nit from the post-merge review.)
                fields={field: copy.deepcopy(expected)},
            ),
            risk_tag=RiskTag(
                blast_radius="bot",
                reversibility="auto",
                touches=["auth_config"],
            ),
            # Typed config edit; applier verifies the write and
            # snapshots the prior value for revert. No metric claim
            # — verification is "permission_monitor stops re-firing
            # this field on the next pass".
            claim=None,
            approval_audience="pod_operator",
            urgency="hygiene",
            admin_surface_summary=headline[:120],
            motivating_signals=[sig_id],
            # ── Phase C-6 operator-first content (Tier 1 — auto-apply) ──
            summary=summary,
            explanation=explanation,
            action_label="Restore baseline",
            manual_path=f"Permissions → {bot_name}",
            dismiss_signature=dismiss_signature_for_field(field),
            dismiss_scope="kind",
        ))

    return proposals
