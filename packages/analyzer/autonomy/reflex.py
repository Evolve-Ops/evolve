"""autonomy.reflex — the auto-demotion reflex (spec §3.3, option b).

One rung down, ``autonomous_within_rules`` → ``act_with_approval``
ONLY, on a deliberately short in-code trigger list:

  1. **Limit-hit escalation** — the bot attempted outward actions on an
     integration ≥ ``ESCALATION_ATTEMPTS`` times AFTER its daily-cap
     pause was mechanically rendered (attempts against a wall = a
     misbehaving or injected agent probing its cage). Counted from the
     bot-side ledger, scoped to the active pause window (≤ 24 h by
     construction — pauses clear at the UTC day roll).
  2. **A critical security finding that names the integration** — a
     firing ``severity="alert"`` Signal from a security producer
     (``security_warden`` / ``audit``) whose ``details.integration_id``
     equals this integration. Bot-wide hygiene findings don't match by
     design: the reflex keys on the exact field, never on prose.

  The bot's cost-enforcement flag does **not** demote — the cost
  breaker already halts the bot; double-punishing posture would
  conflate two mechanisms (spec §3.3 trigger 3). Mechanically: no cost
  producer is in ``_SECURITY_PRODUCERS`` and cost signals carry no
  ``details.integration_id``.

The demotion writes ``set_by: auto_demotion:<signal_id>`` through the
normal store CAS (only ever from rung 3 — a race with an operator
demotion loses cleanly), re-renders, and fires ``autonomy_demoted`` as
a 🔴 alert (the ⚡ breaker carve-out deliberately does NOT apply) whose
one-click restore is a Remediation button. Restore is a promotion and
therefore asks for confirmation; the cleared rules block is preserved
in the demotion's history record (``prior_rules``) so restore can
resurrect it.

The ``autonomy_demoted`` condition persists until the operator acts:
while ``set_by.actor`` still starts with ``auto_demotion:`` the finding
keeps firing on every pass (fresh demotion or re-derive — same
signature, the store dedups), and any deliberate operator action on the
integration rewrites ``set_by`` and lets the sweep resolve it.

The reflex never fights a human: trigger evidence must POSTDATE the
posture's ``set_at``. An operator who reviews a trigger and restores
the level has made an informed decision about exactly that evidence —
re-demoting off the same attempts (or the same still-firing finding)
would loop the restore button. Fresh evidence after the restore
(new attempts, a re-observed finding) trips the reflex again.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import actions_ledger as _ledger
from . import catalog as _catalog
from . import limits as _limits
from . import store as _store


DEMOTED_SIGNAL_TYPE = "autonomy_demoted"

# Trigger 1: attempts after the pause render within the active pause
# window. 3 = more than a single confused retry, small enough to act
# during an incident.
ESCALATION_ATTEMPTS = 3

# Trigger 2: producers whose alert-severity findings can demote. In-code
# and deliberately short (spec §3.3): security findings only — cost
# producers are excluded by construction.
_SECURITY_PRODUCERS = frozenset({"security_warden", "audit"})

RESTORE_REMEDIATION_KIND = "restore_autonomy_posture"


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _attempts_after_pause(
    actions: list[_ledger.OutwardAction],
    integration_id: str,
    paused_at: str,
) -> int:
    """Outward-tool attempts (any result — a denied call errors) on this
    integration strictly after the pause was recorded."""
    return sum(
        1 for a in actions
        if a.integration_id == integration_id and a.ts > paused_at
    )


def _critical_security_signal(
    shared_dir: Path, bot_id: str, integration_id: str,
    *, created_after: str = "",
) -> Any | None:
    """A firing alert-severity security Signal naming this integration
    that AROSE after ``created_after`` (the posture's set_at).

    The floor is ``created_at``, deliberately NOT ``last_observed_at``:
    sweep-style producers bump last_observed_at on every pass, so a
    still-firing finding the operator already reviewed (and restored
    over) would re-trip the reflex minutes later — the restore-button
    loop. A genuinely new finding gets a new Signal (new created_at)
    and demotes again.
    """
    try:
        from signals import store as signals_store
    except ImportError:
        return None
    try:
        for sig in signals_store.iter_active(
            shared_dir, severity="alert", state="firing", bot_id=bot_id,
        ):
            if sig.producer not in _SECURITY_PRODUCERS:
                continue
            details = sig.details if isinstance(sig.details, dict) else {}
            if details.get("integration_id") != integration_id:
                continue
            created = str(getattr(sig, "created_at", "") or "")
            if created_after and created and created <= created_after:
                continue
            return sig
    except Exception:  # noqa: BLE001 — a broken store must not stop the pass
        return None
    return None


def run_bot(
    shared_dir: Path,
    bot_id: str,
    config: dict | None = None,
    *,
    home_override: Path | None = None,
    now: datetime | None = None,
    act: bool = True,
) -> tuple[list[dict[str, Any]], bool]:
    """Evaluate + apply the demotion reflex for one bot.

    Returns ``(findings, ran_ok)`` — the permission-monitor contract.
    Idempotent: a posture already demoted (``auto_demotion:`` actor)
    only re-derives its pending-review finding; the write path only
    ever fires from rung 3 via CAS.

    ``act=False`` is the dry-run path: only re-derives pending-review
    findings for already-demoted postures — it never evaluates the
    triggers, because reporting "stepped down" without stepping down
    would be a false alert.
    """
    now = _now(now)
    findings: list[dict[str, Any]] = []
    try:
        doc = _store.load(shared_dir, bot_id)
    except ValueError:
        return findings, False  # coherence owns the malformed-file finding
    if doc is None or not doc.integrations:
        return findings, True

    try:
        actions = _ledger.read_outward_actions(
            shared_dir, bot_id, window_days=2, now=now,
        )
    except Exception:  # noqa: BLE001
        return findings, False
    limit_entries = _limits.load_limits(shared_dir, bot_id)

    ran_ok = True
    for iid, posture in sorted(doc.integrations.items()):
        actor = (posture.set_by or {}).get("actor") or ""
        if actor.startswith(_store.ACTOR_PREFIX_AUTO_DEMOTION):
            findings.append(_demoted_finding(bot_id, posture))
            continue
        if not act:
            continue
        if posture.rung != _catalog.RUNG_AUTONOMOUS:
            continue
        if actor == _store.ACTOR_BACKFILL:
            continue  # observe-only postures are never rendered or demoted

        trigger_signal_id = ""
        reason = ""
        entry = limit_entries.get(iid) or {}
        # Evidence the operator already ruled on never re-demotes: only
        # attempts/observations after the posture's own set_at count, so
        # a confirmed restore isn't immediately re-tripped by the same
        # pre-restore probing (see module docstring).
        evidence_floor = max(
            str(entry.get("paused_at") or ""), posture.set_at or "",
        )
        if (
            entry.get("paused")
            and entry.get("date") == _ledger.utc_today(now)
            and entry.get("paused_at")
            and _attempts_after_pause(actions, iid, evidence_floor)
            >= ESCALATION_ATTEMPTS
        ):
            sig = _find_own_signal(
                shared_dir, bot_id, iid, _limits.LIMIT_SIGNAL_TYPE,
            )
            trigger_signal_id = getattr(sig, "id", "") or "limit_escalation"
            reason = (
                "kept attempting outward actions after the daily limit "
                "paused it"
            )
        else:
            sig = _critical_security_signal(
                shared_dir, bot_id, iid, created_after=posture.set_at or "",
            )
            if sig is not None:
                trigger_signal_id = sig.id
                reason = "a critical security finding names this integration"

        if not trigger_signal_id:
            continue

        prior_rules = dict(posture.rules or {})
        try:
            demoted = _store.set_posture(
                shared_dir, bot_id, iid,
                rung=_catalog.RUNG_ACT_WITH_APPROVAL,
                rules={},
                actor=f"{_store.ACTOR_PREFIX_AUTO_DEMOTION}{trigger_signal_id}",
                note=f"automatic safety step-down: {reason}",
                expected_current_rung=_catalog.RUNG_AUTONOMOUS,
                history_extra={"prior_rules": prior_rules},
            )
        except _store.StalePostureError:
            continue  # someone changed it first — their decision wins
        except ValueError:
            ran_ok = False
            continue

        from . import renderer as _renderer
        result = _renderer.render_bot(
            bot_id, shared_dir, home_override=home_override, now=now,
        )
        if result.write_error:
            import sys
            print(
                f"[autonomy/reflex] {bot_id}/{iid}: demotion render failed: "
                f"{result.write_error}",
                file=sys.stderr,
            )
        findings.append(_demoted_finding(
            bot_id, demoted, fresh_reason=reason,
            trigger_signal_id=trigger_signal_id,
        ))

    return findings, ran_ok


def _find_own_signal(
    shared_dir: Path, bot_id: str, integration_id: str, sig_type: str,
) -> Any | None:
    try:
        from signals import store as signals_store
    except ImportError:
        return None
    try:
        for sig in signals_store.iter_active(shared_dir, bot_id=bot_id):
            if sig.type != sig_type:
                continue
            details = sig.details if isinstance(sig.details, dict) else {}
            if details.get("integration_id") == integration_id:
                return sig
    except Exception:  # noqa: BLE001
        return None
    return None


def _last_demotion_record(posture: _store.IntegrationPosture) -> dict[str, Any]:
    for record in reversed(posture.history or []):
        actor = str(record.get("actor") or "")
        if actor.startswith(_store.ACTOR_PREFIX_AUTO_DEMOTION):
            return record
    return {}


def _demoted_finding(
    bot_id: str,
    posture: _store.IntegrationPosture,
    *,
    fresh_reason: str = "",
    trigger_signal_id: str = "",
) -> dict[str, Any]:
    """The ``autonomy_demoted`` finding — same signature whether emitted
    at demotion time or re-derived by a later monitor pass, so the
    Signal store keeps one firing Signal until the operator acts."""
    binding = _catalog.binding_for(posture.integration_id)
    spec = _catalog.kind_spec(posture.kind)
    display = binding.display_name if binding else posture.integration_id
    noun = spec.operator_noun if spec else posture.kind
    record = _last_demotion_record(posture)
    prior_rules = record.get("prior_rules")
    prior_rules = dict(prior_rules) if isinstance(prior_rules, dict) else {}
    reason = fresh_reason or (
        str(record.get("note") or "").removeprefix("automatic safety step-down: ")
        or "a safety condition fired"
    )
    actor = (posture.set_by or {}).get("actor") or ""
    trigger = trigger_signal_id or actor.removeprefix(
        _store.ACTOR_PREFIX_AUTO_DEMOTION
    )
    consequence = (
        spec.promotion_consequences.get(_catalog.RUNG_AUTONOMOUS, "")
        if spec else ""
    )
    return {
        "type": DEMOTED_SIGNAL_TYPE,
        "severity": "alert",
        "signature_scope": f"{bot_id}:{posture.integration_id}",
        "title": (
            f"{bot_id}: {noun} ({display}) stepped down to "
            f"\"{_catalog.RUNG_LABELS[_catalog.RUNG_ACT_WITH_APPROVAL]}\""
        ),
        "body": (
            f"Evolve reduced what {bot_id} can do with {noun} on {display} "
            f"because {reason}. It now asks before every {noun} action "
            "instead of acting on its own. Review the trigger, then either "
            "keep the safer setting or restore the previous one below — "
            "restoring asks you to confirm."
        ),
        "details": {
            "bot_id": bot_id,
            "integration_id": posture.integration_id,
            "integration_label": f"{noun} ({display})",
            "kind": posture.kind,
            "rung": posture.rung,
            "rung_label": _catalog.RUNG_LABELS.get(posture.rung, posture.rung),
            "demoted_from": _catalog.RUNG_AUTONOMOUS,
            "prior_rules": prior_rules,
            "trigger_signal_id": trigger,
            "reason": reason,
            "demoted_at": record.get("at") or posture.set_at,
        },
        "remediation": {
            "kind": RESTORE_REMEDIATION_KIND,
            "params": {
                "bot_id": bot_id,
                "integration_id": posture.integration_id,
                "rung": _catalog.RUNG_AUTONOMOUS,
                "rules": prior_rules,
                "expected_current_rung": _catalog.RUNG_ACT_WITH_APPROVAL,
            },
            "label": "Restore previous level",
            "confirm": (
                (consequence + " ") if consequence else ""
            ) + "This puts the integration back exactly where it was "
                "before the automatic step-down.",
        },
    }


__all__ = [
    "DEMOTED_SIGNAL_TYPE",
    "ESCALATION_ATTEMPTS",
    "RESTORE_REMEDIATION_KIND",
    "run_bot",
]
