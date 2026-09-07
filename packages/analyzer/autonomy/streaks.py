"""autonomy.streaks — the promotion-streak producer (spec §3.2 step 1).

Pure Python, no LLM, no transcript reading. Emits the
``autonomy_promotion_candidate`` condition the ``autonomy_promoter``
generator subscribes to; the condition is operator-visible on the
Alerts page before any Proposal exists (principle-signals-precede-
proposals).

Honest v1 sourcing — what the evidence actually is
--------------------------------------------------
The only structured record of outward actions is the bot-side ledger
(``autonomy.actions_ledger``; OQ-3). At "Asks first" the per-action
approval is the rung's operating contract (procedural — OC has no
per-tool ask gate, spec §8 OQ-1 re-checked 2026-06-11), and edits to a
draft before the OK happen in conversation, which Evolve never reads.
So v1's streak is, precisely: *N outward actions performed at "Asks
first" over a span of ≥ M days, with zero autonomy incidents recorded
for the integration in the window*. The candidate copy says "with your
OK" because that is the rung's contract; it does not claim Evolve
verified each OK, and "approved-without-edit" cannot be measured
without transcripts — the proposal pitch carries the same framing.

Consequences of the sourcing, stated plainly:

  - only ``act_with_approval`` → ``autonomous_within_rules`` candidates
    exist in v1. At "Drafts only" outward tools are denied, the human
    performs the action, and nothing structured records it — a
    rung-1 → rung-2 streak has no honest data source yet.
  - a bot whose sends all happened while NOT at "Asks first" (e.g.
    before a demotion) contributes nothing: only actions timestamped
    after the current posture's ``set_at`` count.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import actions_ledger as _ledger
from . import catalog as _catalog
from . import limits as _limits
from . import reflex as _reflex
from . import store as _store


PROMOTION_SIGNAL_TYPE = "autonomy_promotion_candidate"

# Streak thresholds (spec §3.2's N over ≥ M days). 10 actions across a
# week of real use is sustained-enough evidence to be worth a proposal;
# the 30-day window keeps one ancient burst from qualifying forever.
STREAK_WINDOW_DAYS = 30
STREAK_MIN_ACTIONS = 10
STREAK_MIN_SPAN_DAYS = 7

# Incident types that veto a candidate while firing for the integration.
_INCIDENT_TYPES = frozenset({
    _limits.LIMIT_SIGNAL_TYPE,
    _reflex.DEMOTED_SIGNAL_TYPE,
    "autonomy_posture_drift",
})


def candidates(
    shared_dir: Path,
    bot_id: str,
    *,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Streak findings for one bot. ``(findings, ran_ok)`` — the
    permission-monitor contract."""
    now = now or datetime.now(timezone.utc)
    findings: list[dict[str, Any]] = []
    try:
        doc = _store.load(shared_dir, bot_id)
    except ValueError:
        return findings, False
    if doc is None or not doc.integrations:
        return findings, True

    try:
        actions = _ledger.read_outward_actions(
            shared_dir, bot_id, window_days=STREAK_WINDOW_DAYS, now=now,
        )
    except Exception:  # noqa: BLE001 — unreadable ledger = tooling failure
        return findings, False

    incident_iids = _firing_incident_integrations(shared_dir, bot_id)

    for iid, posture in sorted(doc.integrations.items()):
        if posture.rung != _catalog.RUNG_ACT_WITH_APPROVAL:
            continue
        actor = (posture.set_by or {}).get("actor") or ""
        if actor == _store.ACTOR_BACKFILL:
            # Observe-only postures aren't deliberate yet — confirming
            # the current state comes before proposing to widen it.
            continue
        if actor.startswith(_store.ACTOR_PREFIX_AUTO_DEMOTION):
            # Freshly auto-demoted: proposing re-promotion would fight
            # the reflex. The operator's restore path owns this.
            continue
        if iid in incident_iids:
            continue
        spec = _catalog.kind_spec(posture.kind)
        binding = _catalog.binding_for(iid)
        if spec is None or binding is None:
            continue

        # Only actions performed at THIS posture count as streak
        # evidence — set_at is when "Asks first" became the rung.
        performed = [
            a for a in actions
            if a.integration_id == iid and a.result != "error"
            and (not posture.set_at or a.ts >= posture.set_at)
        ]
        if len(performed) < STREAK_MIN_ACTIONS:
            continue
        days = sorted({a.day for a in performed})
        span_days = _span_days(days[0], days[-1])
        if span_days is None or span_days < STREAK_MIN_SPAN_DAYS:
            continue

        per_day_max = max(
            sum(1 for a in performed if a.day == d) for d in days
        )
        display = binding.display_name
        noun = spec.operator_noun
        findings.append({
            "type": PROMOTION_SIGNAL_TYPE,
            "severity": "info",
            "signature_scope": f"{bot_id}:{iid}",
            "title": (
                f"{bot_id}: {noun} ({display}) has a clean track record "
                "at \"Asks first\""
            ),
            "body": (
                f"{bot_id} has performed {len(performed)} {noun} actions "
                f"on {display} over the last {span_days + 1} days at "
                "\"Asks first\" — each one with your go-ahead, per its "
                "operating rules — and no open incidents. A suggestion "
                "to allow it to act within limits may follow on the "
                "Improvements page; nothing changes unless you approve "
                "it there."
            ),
            "details": {
                "bot_id": bot_id,
                "integration_id": iid,
                "integration_label": f"{noun} ({display})",
                "kind": posture.kind,
                "rung": posture.rung,
                "rung_label": _catalog.RUNG_LABELS.get(posture.rung, posture.rung),
                "actions": len(performed),
                "distinct_days": len(days),
                "span_days": span_days + 1,
                "window_days": STREAK_WINDOW_DAYS,
                "first_day": days[0],
                "last_day": days[-1],
                "max_actions_per_day": per_day_max,
                # Conservative cap suggestion for the generator's rules
                # block: double the busiest observed day, floor of 5.
                "suggested_actions_per_day": max(5, per_day_max * 2),
                "next_rung": _catalog.RUNG_AUTONOMOUS,
                "next_rung_label": _catalog.RUNG_LABELS[_catalog.RUNG_AUTONOMOUS],
            },
        })

    return findings, True


def _span_days(first_day: str, last_day: str) -> int | None:
    try:
        from datetime import date
        return (date.fromisoformat(last_day) - date.fromisoformat(first_day)).days
    except ValueError:
        return None


def _firing_incident_integrations(shared_dir: Path, bot_id: str) -> set[str]:
    try:
        from signals import store as signals_store
    except ImportError:
        return set()
    out: set[str] = set()
    try:
        for sig in signals_store.iter_active(
            shared_dir, bot_id=bot_id, state="firing",
        ):
            if sig.type not in _INCIDENT_TYPES:
                continue
            details = sig.details if isinstance(sig.details, dict) else {}
            iid = details.get("integration_id")
            if isinstance(iid, str) and iid:
                out.add(iid)
    except Exception:  # noqa: BLE001 — veto check is best-effort
        return out
    return out


__all__ = [
    "PROMOTION_SIGNAL_TYPE",
    "STREAK_MIN_ACTIONS",
    "STREAK_MIN_SPAN_DAYS",
    "STREAK_WINDOW_DAYS",
    "candidates",
]
