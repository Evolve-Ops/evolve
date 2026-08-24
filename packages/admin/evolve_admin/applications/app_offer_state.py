"""app_offer_state — what the bot has (or has not) asked its user about a draft.

AL-1.8b, design §3 (the Discovered tab "shows the state of the offer") and
D-U3. **Read-only.** AL-1.7 built the offer — the promotion Proposal, the
"never" shield, the snooze — and nothing on the admin surface showed it. This
module is that display half and adds no write path of its own.

WHERE THE STATE ACTUALLY LIVES, which is two places and not one:

  * **The manifest** carries the user's standing answers —
    ``do_not_offer`` / ``do_not_offer_by`` (the permanent "never", design
    §7.2) and ``promotion_snoozed_until`` (a deferral). Both are durable
    across a re-mint by construction (``app_promotion``'s field docstrings).
  * **The Proposal store** carries the offers themselves. AL-1.7 makes a
    promotion a Proposal, so "was this draft offered, when, to whom, and what
    came back" is answered by the proposals whose ``generator_id`` is
    ``app_promotion`` and whose ``provenance.signals.manifest_stem`` names
    the draft.

THE FIFTH STATE IS ``unknown``, AND IT IS THE POINT. When the proposal store
cannot be read, "no offer found" and "we could not look" are the same
observation with opposite meanings, and rendering the second as "not yet
offered" is exactly the silent degradation principle-tri-state-status is
written against. So a failed read returns ``unknown`` for every draft that
carries no manifest-side answer, and the surface says the pod could not
check.

Precedence, strongest first: ``never`` (the user said no, permanently) →
``snoozed`` (the user deferred, and the deferral has not expired) →
``offered`` (a proposal exists) → ``not_offered`` / ``unknown``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

log = logging.getLogger(__name__)

__all__ = [
    "OFFER_NEVER",
    "OFFER_NOT_OFFERED",
    "OFFER_OFFERED",
    "OFFER_SNOOZED",
    "OFFER_UNKNOWN",
    "build_offer_index",
    "offer_state_for",
]

OFFER_NEVER = "never"
OFFER_SNOOZED = "snoozed"
OFFER_OFFERED = "offered"
OFFER_NOT_OFFERED = "not_offered"
OFFER_UNKNOWN = "unknown"

#: Every subdir a promotion Proposal can be sitting in. The same list
#: ``app_promotion_sweep`` reads for the cadence cap — counting only
#: ``pending`` would make an answered offer look like one that never
#: happened.
_SUBDIRS = ("pending", "snoozed", "applied", "archived")

#: Proposal statuses that mean the user (or the operator) turned the offer
#: down. Everything else is carried through as-is rather than bucketed: the
#: surface says "offered", and the raw outcome rides along for the tooltip.
_DECLINED = {"rejected", "dismissed"}


def _s(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _parse_iso(raw: Any) -> "datetime | None":
    text = _s(raw)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def build_offer_index(shared_dir: Path) -> "tuple[dict[tuple[str, str], dict], bool]":
    """``({(bot_id, manifest_stem): newest offer}, readable)``.

    One pass over the proposal store per request, rather than a lookup per
    draft: the Discovered list asks for every draft on the pod at once.

    ``readable`` is False when the store could not be read at all. Callers
    turn that into ``unknown`` rather than into "not yet offered" — see the
    module docstring.
    """
    index: "dict[tuple[str, str], dict]" = {}
    try:
        from arbiter import store as _astore

        from .app_promotion import is_promotion_proposal
    except Exception as exc:  # noqa: BLE001 — reported as unknown, never as "none"
        log.info("app_offer_state: proposal store unavailable (%s)", exc)
        return index, False
    try:
        proposals = list(_astore.iter_proposals(shared_dir, subdirs=_SUBDIRS))
    except Exception as exc:  # noqa: BLE001
        log.warning("app_offer_state: could not read the proposal store: %s", exc)
        return index, False

    for proposal in proposals:
        if not is_promotion_proposal(proposal):
            continue
        bot_id = _s(getattr(proposal, "bot_id", ""))
        signals = getattr(getattr(proposal, "provenance", None), "signals", None) or {}
        stem = _s(signals.get("manifest_stem")) if isinstance(signals, Mapping) else ""
        if not bot_id or not stem:
            continue
        entry = {
            "proposal_id": _s(getattr(proposal, "id", "")),
            "at": _s(getattr(proposal, "created_at", "")) or None,
            "outcome": _s(getattr(proposal, "status", "")) or None,
            "to": _s(getattr(proposal, "approval_audience", "")) or None,
            "title": _s(getattr(proposal, "human_title", "")) or None,
        }
        key = (bot_id, stem)
        prior = index.get(key)
        # Newest wins. A missing timestamp sorts oldest rather than
        # crashing the comparison — an unreadable date is not a reason to
        # drop the fact that an offer happened.
        if prior is None or (entry["at"] or "") >= (prior["at"] or ""):
            index[key] = entry
    return index, True


def offer_state_for(
    manifest: Mapping[str, Any],
    *,
    bot_id: str,
    manifest_stem: str,
    offers: "Mapping[tuple[str, str], dict] | None",
    offers_readable: bool,
    now: "datetime | None" = None,
) -> dict[str, Any]:
    """The offer envelope for one draft. Never invents an answer."""
    from .app_promotion import (
        DO_NOT_OFFER_BY_FIELD,
        DO_NOT_OFFER_FIELD,
        SNOOZE_UNTIL_FIELD,
    )

    now = now or datetime.now(timezone.utc)
    data: Mapping[str, Any] = manifest if isinstance(manifest, Mapping) else {}
    offer = (offers or {}).get((bot_id, manifest_stem))

    if bool(data.get(DO_NOT_OFFER_FIELD)):
        return {
            "state": OFFER_NEVER,
            "by": _s(data.get(DO_NOT_OFFER_BY_FIELD)) or None,
            "at": (offer or {}).get("at"),
            "until": None,
            "outcome": (offer or {}).get("outcome"),
            "to": (offer or {}).get("to"),
        }

    until = _parse_iso(data.get(SNOOZE_UNTIL_FIELD))
    if until is not None and until > now:
        return {
            "state": OFFER_SNOOZED,
            "by": None,
            "at": (offer or {}).get("at"),
            "until": _s(data.get(SNOOZE_UNTIL_FIELD)),
            "outcome": (offer or {}).get("outcome"),
            "to": (offer or {}).get("to"),
        }

    if offer is not None:
        return {
            "state": OFFER_OFFERED,
            "by": None,
            "at": offer.get("at"),
            "until": None,
            "outcome": offer.get("outcome"),
            "declined": offer.get("outcome") in _DECLINED,
            "to": offer.get("to"),
        }

    if not offers_readable:
        return {"state": OFFER_UNKNOWN, "by": None, "at": None, "until": None,
                "outcome": None, "to": None}
    return {"state": OFFER_NOT_OFFERED, "by": None, "at": None, "until": None,
            "outcome": None, "to": None}
