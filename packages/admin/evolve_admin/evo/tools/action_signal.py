"""Action tools — signals.

Exposes two write_safe action tools that move signals through their
state machine:

* ``action.signal.snooze(signal_id, duration?, reason?)`` — firing →
  snoozed. Defaults to 24h when no duration given. Recoverable (signal
  auto-wakes at snoozed_until OR the operator can dismiss / un-snooze
  manually).

* ``action.signal.dismiss(signal_id, verdict?, reason?)`` — firing →
  dismissed. Terminal "don't show again." When verdict is one of
  {false_positive, bad_inference, not_actionable}, also writes
  signals/feedback.jsonl for any proposals motivated by this signal —
  same shape as the admin UI's dismiss flow at
  /api/signals/<id>/dismiss.

Both tools are write_safe under the spec's risk tiering (§2.2):
  - Reversible (un-snooze on wake; dismiss can be re-fired by a
    re-observe within the reopen window, though dismissed signals
    aren't auto-reopened by observe()).
  - Bounded blast radius (one signal at a time).
  - Auto-execute under 'auto-small' and 'auto' authority tiers; render
    a confirm button under 'ask'.

Validate contract (per spec §5.2, third gate before button renders):
* ``validate`` confirms the signal exists and is in the right
  starting state — for snooze, must be firing; for dismiss, must be
  firing or snoozed (both allow dismiss per state_machine.transition).
* Returns {ok: True} when the action can proceed; {ok: False,
  reason: ...} otherwise. The admin-UI proxy reads this before
  rendering a confirmation button so the operator never sees a
  button that would have errored.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


# Duration parsing — mirrors the helper in evo/handlers/mute.py and
# the admin UI's _parse_duration. Accepts "<N>h", "<N>d", "<N>w" with
# integer N. Anything else (or N<=0) returns None — the validate
# step surfaces a clean error.
_DURATION_RE = re.compile(r"^\s*(\d+)\s*([hdw])\s*$", re.IGNORECASE)


def _parse_duration(raw: str | None) -> timedelta | None:
    if not raw:
        return None
    m = _DURATION_RE.match(raw)
    if not m:
        return None
    n = int(m.group(1))
    if n <= 0:
        return None
    unit = m.group(2).lower()
    if unit == "h":
        return timedelta(hours=n)
    if unit == "d":
        return timedelta(days=n)
    if unit == "w":
        return timedelta(weeks=n)
    return None


def _find_signal(shared_dir: Path, signal_id: str):
    """Locate a signal by id across firing/snoozed subdirs.

    Wraps signals.store.find_signal. Returns the Signal or None. The
    handler + validate share this lookup so a single existence check
    informs both paths.
    """
    try:
        from signals import store as signals_store
    except ImportError as exc:
        log.warning("action.signal.*: signals store unavailable: %s", exc)
        return None
    located = signals_store.find_signal(shared_dir, signal_id)
    if located is None:
        return None
    sig, _path, _subdir = located
    return sig


# ─── action.signal.snooze ───────────────────────────────────────────────────


def _snooze_handler(
    shared_dir: Path,
    signal_id: str,
    duration: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Snooze a firing signal.

    Duration defaults to 24h when omitted (matches the admin UI's
    /api/signals/<id>/snooze behavior). Reason defaults to 'evo
    snoozed' so the audit trail shows the action came from evo's
    surface rather than direct UI / Telegram.
    """
    try:
        from signals import store as signals_store
    except ImportError as exc:
        return {"ok": False, "error": f"signals store unavailable: {exc}"}

    sig = _find_signal(shared_dir, signal_id)
    if sig is None:
        return {"ok": False, "error": f"signal '{signal_id}' not found"}

    delta = _parse_duration(duration) or timedelta(hours=24)
    snoozed_until = (datetime.now(timezone.utc) + delta).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")

    try:
        signals_store.apply_transition(
            sig, "snoozed", shared_dir,
            actor="evo",
            reason=(reason or "evo snoozed via tool"),
            snoozed_until=snoozed_until,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("action.signal.snooze: transition failed")
        return {"ok": False, "error": f"transition failed: {exc}"}

    return {
        "ok": True,
        "signal_id": sig.id,
        "to_state": "snoozed",
        "snoozed_until": snoozed_until,
        "title": sig.title,
        # Post-action verify hint (spec §3.7 reliability lever #4).
        # The model should call this read tool to confirm the signal
        # moved out of firing/ and into snoozed/ — guards against race
        # conditions and partial writes. AGENTS.md "Post-action
        # verify" section teaches the broader pattern.
        "verify_via": {
            "tool": "pod_state.signals.history",
            "args": {"signal_id": sig.id, "state": "snoozed"},
            "expect": "this signal_id appears with state=snoozed",
        },
    }


def _snooze_validate(
    shared_dir: Path,
    signal_id: str,
    duration: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Dry-run: confirm the signal exists and is in firing state, and
    that duration parses cleanly. Returns {ok: True} when snooze can
    fire; {ok: False, reason: ...} otherwise.

    Per spec §5.2 third gate: when this returns ok=False, the proxy
    skips rendering a button and includes the reason in evo's reply
    so the model can explain what went wrong.
    """
    if not signal_id:
        return {"ok": False, "reason": "signal_id is required"}

    if duration is not None and _parse_duration(duration) is None:
        return {
            "ok": False,
            "reason": (
                f"duration {duration!r} is invalid — use formats like "
                "'1h', '24h', '7d', '2w'"
            ),
        }

    sig = _find_signal(shared_dir, signal_id)
    if sig is None:
        return {"ok": False, "reason": f"signal '{signal_id}' not found"}

    if sig.state != "firing":
        return {
            "ok": False,
            "reason": (
                f"signal is in state '{sig.state}', not 'firing'; "
                "snooze only applies to firing signals"
            ),
        }

    return {"ok": True, "context": {"current_title": sig.title}}


SNOOZE_TOOL = Tool(
    name="action.signal.snooze",
    description=(
        "Snooze a firing signal so it stops appearing in the alerts "
        "queue until the snooze window expires (or the operator "
        "manually wakes it). Use for signals that are real but "
        "currently being handled outside the pod (e.g. you're "
        "working on the underlying fix), or to defer non-urgent "
        "noise. The signal auto-wakes back to firing when "
        "snoozed_until is reached. Reversible — operator can "
        "dismiss or wait it out. Default duration 24h if not "
        "specified."
    ),
    wire_description=(
        "Snooze a firing signal out of the alerts queue until the window "
        "expires (auto-wakes at snoozed_until; default 24h). Use for "
        "signals that are real but being handled, or to defer non-urgent "
        "noise. Reversible — the operator can dismiss or wait it out."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "signal_id": {
                "type": "string",
                "description": (
                    "Signal id (UUID) — get from "
                    "pod_state.signals.firing."
                ),
            },
            "duration": {
                "type": "string",
                "description": (
                    "How long to snooze, as '<N>h', '<N>d', or "
                    "'<N>w' (hours/days/weeks). Default '24h'. "
                    "Examples: '1h', '4h', '24h', '7d', '2w'."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "One-line reason for the snooze, recorded in the "
                    "signal's state_history. Visible in the alert "
                    "history on the admin UI."
                ),
            },
        },
        "required": ["signal_id"],
        "additionalProperties": False,
    },
    handler=_snooze_handler,
    risk_tier=RiskTier.WRITE_SAFE,
    validate=_snooze_validate,
    tags=("action", "signal"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(SNOOZE_TOOL)


# ─── action.signal.dismiss ──────────────────────────────────────────────────

# Verdict shape mirrors the admin UI's /api/signals/<id>/dismiss
# semantics: when one of these three is provided, the signal-feedback
# log gets an entry for each proposal motivated by this signal so
# producers can tune detection later.
_VALID_VERDICTS = frozenset({"false_positive", "bad_inference", "not_actionable"})


def _dismiss_handler(
    shared_dir: Path,
    signal_id: str,
    verdict: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Dismiss a signal (terminal — 'don't show again').

    When verdict is provided, also writes feedback for each motivated
    proposal so producers can tune detection. Mirrors the admin UI's
    /api/signals/<id>/dismiss flow.
    """
    try:
        from signals import store as signals_store
    except ImportError as exc:
        return {"ok": False, "error": f"signals store unavailable: {exc}"}

    sig = _find_signal(shared_dir, signal_id)
    if sig is None:
        return {"ok": False, "error": f"signal '{signal_id}' not found"}

    try:
        signals_store.apply_transition(
            sig, "dismissed", shared_dir,
            actor="evo",
            reason=(reason or "evo dismissed via tool"),
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("action.signal.dismiss: transition failed")
        return {"ok": False, "error": f"transition failed: {exc}"}

    # Best-effort feedback write when a verdict is provided. Same
    # behavior as the admin UI dismiss handler.
    feedback_written = 0
    if verdict in _VALID_VERDICTS:
        try:
            proposals = list(sig.motivated_proposals or [None])
            for prop_id in proposals:
                signals_store.write_feedback(
                    shared_dir,
                    signal_id=sig.id,
                    signal_signature=sig.signature,
                    proposal_id=prop_id or "",
                    verdict=verdict,
                    note=(reason or "").strip(),
                )
                feedback_written += 1
        except Exception as exc:  # noqa: BLE001
            # Feedback failure is non-blocking — the dismiss already
            # succeeded; log and continue.
            log.warning("action.signal.dismiss: feedback write failed: %s", exc)

    out = {
        "ok": True,
        "signal_id": sig.id,
        "to_state": "dismissed",
        "title": sig.title,
        # Post-action verify hint (spec §3.7 reliability lever #4).
        # Dismissed signals land in the archived/ subdir; the model
        # confirms by calling pod_state.signals.history(state=
        # "dismissed") and looking for the signal id. AGENTS.md
        # teaches the broader pattern.
        "verify_via": {
            "tool": "pod_state.signals.history",
            "args": {"signal_id": sig.id, "state": "dismissed"},
            "expect": "this signal_id appears with state=dismissed",
        },
    }
    if verdict:
        out["verdict"] = verdict
        out["feedback_written"] = feedback_written
    return out


def _dismiss_validate(
    shared_dir: Path,
    signal_id: str,
    verdict: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Dry-run: confirm the signal exists and is in firing or snoozed
    state (both are dismissable per the state machine). Validate
    verdict if provided.
    """
    if not signal_id:
        return {"ok": False, "reason": "signal_id is required"}

    if verdict is not None and verdict not in _VALID_VERDICTS:
        return {
            "ok": False,
            "reason": (
                f"verdict {verdict!r} is invalid — must be one of "
                f"{sorted(_VALID_VERDICTS)} or omitted"
            ),
        }

    sig = _find_signal(shared_dir, signal_id)
    if sig is None:
        return {"ok": False, "reason": f"signal '{signal_id}' not found"}

    if sig.state not in ("firing", "snoozed"):
        return {
            "ok": False,
            "reason": (
                f"signal is in terminal state '{sig.state}'; "
                "dismiss only applies to firing or snoozed signals"
            ),
        }

    return {"ok": True, "context": {"current_title": sig.title}}


DISMISS_TOOL = Tool(
    name="action.signal.dismiss",
    description=(
        "Dismiss a signal terminally — 'I've seen this, don't show "
        "again.' Removes from the alerts queue. When verdict is "
        "provided (false_positive | bad_inference | not_actionable), "
        "also records feedback so the signal's producer can tune "
        "detection. Use when a signal is genuinely a false alarm or "
        "when you've handled the underlying condition outside evo's "
        "reach. Effectively permanent — dismissed signals don't "
        "auto-reopen via re-observation."
    ),
    wire_description=(
        "Dismiss a signal terminally — it leaves the alerts queue and does "
        "not auto-reopen on re-observation. Optionally record a verdict "
        "(false_positive | bad_inference | not_actionable) as "
        "producer-tuning feedback. Use only for genuine false alarms or "
        "conditions handled outside evo's reach; effectively permanent."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "signal_id": {
                "type": "string",
                "description": "Signal id (UUID).",
            },
            "verdict": {
                "type": "string",
                "enum": ["false_positive", "bad_inference", "not_actionable"],
                "description": (
                    "Why this signal is being dismissed. When set, "
                    "writes feedback for any proposals motivated by "
                    "this signal so producers can tune detection."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "One-line operator-facing reason, recorded in "
                    "state_history."
                ),
            },
        },
        "required": ["signal_id"],
        "additionalProperties": False,
    },
    handler=_dismiss_handler,
    risk_tier=RiskTier.WRITE_SAFE,
    validate=_dismiss_validate,
    tags=("action", "signal"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(DISMISS_TOOL)


# ─── action.signal.resolve (Phase 1.5e) ──────────────────────────────────────


def _resolve_handler(
    shared_dir: Path,
    signal_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Mark a firing or snoozed signal as resolved-externally.

    Use when the underlying condition was fixed outside the system
    (operator restarted a service manually, fixed config by hand, etc.)
    and the signal can be archived without an in-system action. Distinct
    from dismiss — dismiss says "don't show me again even if it
    reoccurs"; resolve says "this is fixed now; if it reoccurs, fire it
    again."

    State-machine transition: firing|snoozed → resolved (terminal).
    If the producer re-observes the condition within the reopen
    window, signal.store.observe() will re-fire it as a new signal —
    same signature, same producer, different id.
    """
    try:
        from signals import store as signals_store
    except ImportError as exc:
        return {"ok": False, "error": f"signals store unavailable: {exc}"}

    sig = _find_signal(shared_dir, signal_id)
    if sig is None:
        return {"ok": False, "error": f"signal '{signal_id}' not found"}

    try:
        signals_store.apply_transition(
            sig, "resolved", shared_dir,
            actor="evo",
            reason=(reason or "evo resolved-externally via tool"),
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("action.signal.resolve: transition failed")
        return {"ok": False, "error": f"transition failed: {exc}"}

    return {
        "ok": True,
        "signal_id": sig.id,
        "to_state": "resolved",
        "title": sig.title,
        "reason": reason or "evo resolved-externally via tool",
        # Verify hint: the signal should now appear in pod_state.signals.history
        # with state=resolved.
        "verify_via": {
            "tool": "pod_state.signals.history",
            "args": {"signal_id": sig.id, "state": "resolved"},
            "expect": "this signal_id appears with state=resolved",
        },
    }


def _resolve_validate(
    shared_dir: Path,
    signal_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Dry-run: signal exists and is in firing or snoozed.

    Both states allow → resolved per the state-machine table; only
    already-terminal states (resolved/dismissed) refuse.
    """
    if not signal_id:
        return {"ok": False, "reason": "signal_id is required"}

    sig = _find_signal(shared_dir, signal_id)
    if sig is None:
        return {"ok": False, "reason": f"signal '{signal_id}' not found"}

    if sig.state not in ("firing", "snoozed"):
        return {
            "ok": False,
            "reason": (
                f"signal is in terminal state '{sig.state}'; resolve only "
                "applies to firing or snoozed signals"
            ),
        }

    return {
        "ok": True,
        "context": {
            "current_state": sig.state,
            "current_title": sig.title,
        },
    }


RESOLVE_TOOL = Tool(
    name="action.signal.resolve",
    description=(
        "Mark a signal as resolved (terminal) — use when the operator "
        "fixed the underlying condition outside the system (manual "
        "service restart, hand-edited config, etc.) and the signal "
        "should be archived. Distinct from dismiss: dismiss is "
        "permanent 'don't show again', resolve is 'this is fixed; if "
        "it reoccurs, the producer can re-fire it as a new signal'. "
        "Applies to firing or snoozed signals."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "signal_id": {
                "type": "string",
                "description": "Signal id (eg 's-abc12345').",
            },
            "reason": {
                "type": "string",
                "description": (
                    "Optional reason / context — logged to the signal's "
                    "history and visible on the Alerts page."
                ),
            },
        },
        "required": ["signal_id"],
        "additionalProperties": False,
    },
    handler=_resolve_handler,
    risk_tier=RiskTier.WRITE_SAFE,
    validate=_resolve_validate,
    tags=("action", "signal", "lifecycle"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(RESOLVE_TOOL)
