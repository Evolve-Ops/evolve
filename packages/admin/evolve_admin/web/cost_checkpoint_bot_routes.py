"""Bot-facing cost-checkpoint route — ``POST /api/cost-checkpoint/answer``.

Operator decision D-CC1..4
([internal/decision-cost-cap-checkpoint-2026-09-04.md](../../../../internal/decision-cost-cap-checkpoint-2026-09-04.md)).

When a bot's daily cost cap trips under the default ``checkpoint`` action, its
next interactive turn is held by the Evolve plugin's ``before_agent_run`` gate
behind a fixed, model-free "cap reached" reply offering two choices. This route
is where the answer to that question is RECORDED. The plugin dispatches nothing
to a model on either path; it calls here and renders a fixed confirmation.

THE IDENTITY MODEL (mirrors ``directory_bot_routes`` / ``google_bot_routes``):

  The calling bot is bound **server-side** from the kernel-reported peer uid of
  the unix-socket connection (``peer_auth.resolve_peer_bot_id``) — never from a
  request field, so a bot can only ever answer its OWN checkpoint. TCP requests
  (the admin-UI binding) and unknown peer uids get 403.

  The ANSWERING PERSON arrives as gateway-asserted ``{platform, stable_id}``
  REQUEST FIELDS, and their role is resolved **here**, by
  ``roster_overlay.resolve_role`` — the same resolver the Users page and the
  Layer-2 gate use. The plugin's TS resolver feeds the model hints; this one
  decides the role, so a model cannot name its own role. What it does NOT do is
  bind the SPEAKER: ``platform``/``stable_id`` are trusted on the peer-uid
  boundary, exactly as in ``directory_bot_routes`` / ``google_bot_routes``.
  Anything running as the bot's unix user — including the model through an exec
  tool reaching the admin socket — can therefore assert an id it does not own.
  That is the platform's existing trust boundary for every bot route, not a
  boundary this one adds; binding the sender at the gateway is the longer-term
  fix. D-CC3 makes "continue" **owners-only** (``admin`` + ``primary_user`` per
  design-app-access-2026-08-15.md §3).

WHAT "CONTINUE" BUYS (D-CC3):

  A stated increment on today's cap for this bot until the day boundary,
  recorded in the breaker ledger as an operator override naming who said it and
  when. It is NOT an unlimited day: ``spend_alert`` re-trips with a fresh
  ``pending`` checkpoint once spend crosses ``cap + increment``. Background work
  (heartbeats, scheduled jobs) stays paused — the L1 breaker record is untouched
  by an answer, so "continue" resumes the conversation, not the pod's spending
  on the user's behalf.

"STOP" records ``declined``; every further turn gets the short refusal until the
day boundary. Both answers are idempotent — re-answering just rewrites the same
fields and appends another ledger row.
"""
from __future__ import annotations

import logging
from pathlib import Path

from flask import Flask, jsonify, request
from flask.typing import ResponseReturnValue

from ..config import load_network
from . import peer_auth
from .routes_bot_users import _shared_dir_from_net

log = logging.getLogger(__name__)

#: Roles that may answer "continue". ``owners`` in the access model
#: (design-app-access-2026-08-15.md §3) = admin + primary_user.
OWNER_ROLES: frozenset[str] = frozenset({"admin", "primary_user"})

#: Answer verbs the route accepts, mapped to the stored checkpoint state.
_ANSWER_STATES: dict[str, str] = {
    "continue": "continued",
    "stop": "declined",
}


def _resolve_answerer_role(
    network: dict, shared_dir: Path, bot_id: str, platform: str, stable_id: str,
) -> str:
    """The answering identity's effective role on ``bot_id``.

    Resolved through ``roster_overlay.resolve_role`` — the one Python
    resolver — so the answer to "may this person raise the cap?" is the
    same answer the Users page and the Layer-2 tool gate would give.

    Fails CLOSED: any read or resolution error yields ``participant``,
    which is not an owner. A cap that cannot establish who is asking must
    not grant the raise; the user gets the "ask your owner" reply, which
    is recoverable, rather than an unauthorized ceiling lift, which is not.
    """
    try:
        from .. import roster_overlay as _overlay_mod
        overlay = _overlay_mod.load_overlay(shared_dir, bot_id)
        return _overlay_mod.resolve_role(
            overlay, network, bot_id, platform, stable_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "cost-checkpoint: role resolution for %s on %s failed (%s) — "
            "treating as participant", stable_id, bot_id, exc,
        )
        return "participant"


def register_cost_checkpoint_bot_routes(app: Flask, network_path: Path) -> None:
    """Register the bot-facing ``/api/cost-checkpoint/*`` route."""

    @app.post("/api/cost-checkpoint/answer")
    def api_cost_checkpoint_answer() -> ResponseReturnValue:
        """Record an answer to this bot's pending cost checkpoint.

        Body: ``{"answer": "continue" | "stop", "platform": "telegram",
        "stable_id": "1260193629"}``.

        Returns ``{ok, state, role, authorized, increment_usd, cap_usd,
        expires_at}``. ``authorized=false`` with ``state`` unchanged is the
        non-owner outcome — a 200, not a 403: the plugin renders it as the
        "ask your owner" reply, and a transport-level error would be
        indistinguishable from the daemon being down.
        """
        bot_id = peer_auth.resolve_peer_bot_id(network_path)
        if not bot_id:
            # Fail-closed cross-bot boundary: only a recognized bot peer can
            # answer a checkpoint, and only its own.
            return jsonify({"error": "caller not a recognized bot"}), 403

        body = request.get_json(silent=True) or {}
        answer = str(body.get("answer") or "").strip().lower()
        if answer not in _ANSWER_STATES:
            return jsonify({
                "error": f"answer must be one of {sorted(_ANSWER_STATES)}",
            }), 400
        platform = str(body.get("platform") or "").strip().lower()
        stable_id = str(body.get("stable_id") or "").strip()

        try:
            from breakers import store as _bstore
        except ImportError as exc:  # pragma: no cover — packaging fault
            return jsonify({"error": f"breakers.store unavailable: {exc}"}), 500

        net = load_network(network_path)
        shared_dir = _shared_dir_from_net(net)

        record = _bstore.read_trip(shared_dir, bot_id, "cost")
        if record is None or record.checkpoint is None:
            return jsonify({
                "ok": False, "error": "no checkpoint pending", "state": None,
            }), 409

        # Resolve-or-refuse (the RosterTools G-N2 posture): an unnamed
        # speaker is not an owner. We never fall back to a default platform —
        # that would mis-attribute a foreign sender onto an id-space that
        # holds privileged ids.
        role = (
            _resolve_answerer_role(
                net, shared_dir, bot_id, platform, stable_id,
            )
            if platform and stable_id else "participant"
        )
        state = _ANSWER_STATES[answer]
        answered_by = (
            f"user:{platform}:{stable_id}" if platform and stable_id
            else "user:unresolved"
        )

        if state == "continued" and role not in OWNER_ROLES:
            log.info(
                "cost-checkpoint: %s declined continue from %s (role=%s) — "
                "not an owner", bot_id, answered_by, role,
            )
            return jsonify({
                "ok": True, "authorized": False, "state": record.checkpoint,
                "role": role, "bot_id": bot_id,
            })

        increment = None
        cap_usd = None
        if state == "continued":
            increment, cap_usd = _increment_for(shared_dir, bot_id)

        updated = _bstore.answer_checkpoint(
            shared_dir=shared_dir, scope=bot_id, breaker_type="cost",
            state=state, answered_by=answered_by, role=role,
            increment_usd=increment,
        )
        if updated is None:
            # Raced with a reset / day rollover between the read and the
            # write. The hold is gone either way, so report it plainly.
            return jsonify({
                "ok": False, "error": "checkpoint no longer present",
                "state": None,
            }), 409

        log.info(
            "cost-checkpoint: %s → %s by %s (role=%s, increment=%s)",
            bot_id, state, answered_by, role, increment,
        )
        return jsonify({
            "ok": True,
            "authorized": True,
            "bot_id": bot_id,
            "state": updated.checkpoint,
            "role": role,
            "increment_usd": increment,
            "increment_total_usd": updated.checkpoint_increment_usd,
            "cap_usd": cap_usd,
            "expires_at": updated.expires_at,
        })


def _increment_for(
    shared_dir: Path, bot_id: str,
) -> tuple[float | None, float | None]:
    """``(increment, cap)`` for a continue answer on ``bot_id``.

    ``cap`` is today's enforcement-flag cap — the ceiling in force, which is
    the number the plugin rendered in the cap-reached message.

    The increment is 50% of the BASE cap, not of that ceiling. On a re-trip
    the flag carries ``base + everything already granted today``, so taking
    half of it compounds the grant (+10, +15, +22.50 on a $20 cap) while
    D-CC3 says "+50% of the cap" — a fixed step the operator can predict.
    The base is recovered by subtracting the record's cumulative grant, which
    ``store.trip`` carried forward into the very record that raised the flag,
    so the arithmetic here is the exact inverse of the arithmetic there and
    the "+$X" the user was OFFERED is the "+$X" the ledger records.

    Returns ``(None, None)`` when the flag is unreadable: the answer is still
    recorded (the hold lifts), just without a ceiling raise, which leaves the
    next tick free to re-trip immediately. Better a second checkpoint than an
    invented grant.
    """
    try:
        import spend_caps as _spend_caps  # type: ignore[import]
        flag = _spend_caps.get_active_enforcement(shared_dir, bot_id) or {}
        cap = float(flag.get("cap") or 0.0)
        if cap <= 0:
            return None, None
        base = cap - _granted_so_far(shared_dir, bot_id)
        if base <= 0:
            base = cap
        return _spend_caps.checkpoint_increment_usd(base), cap
    except Exception as exc:  # noqa: BLE001
        log.warning("cost-checkpoint: increment resolve failed: %s", exc)
        return None, None


def _granted_so_far(shared_dir: Path, bot_id: str) -> float:
    """Cumulative checkpoint grant already on today's breaker record ($).

    Zero when there is no record, no grant, or the record cannot be read —
    the fail-safe direction, since a zero here makes the base cap equal the
    flag cap and the increment no larger than today's already-offered step.
    """
    try:
        from breakers import store as _bstore  # type: ignore[import]
        rec = _bstore.read_trip(shared_dir, bot_id, "cost")
    except Exception as exc:  # noqa: BLE001
        log.warning("cost-checkpoint: grant re-read failed: %s", exc)
        return 0.0
    if rec is None:
        return 0.0
    return float(rec.checkpoint_increment_usd or 0.0)
