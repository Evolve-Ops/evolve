"""Admin person-link routes — "this platform id is the same person" (M1-B4a).

The operator surface over :mod:`evolve_admin.roster_identity`'s D1 seam. One
human reaches a bot on Discord today and Telegram tomorrow; those two platform
ids share **nothing** that could link them, so Evolve cannot and must not infer
sameness — roles attach to admitted identities (invariant 2), so a wrong link
is a *privilege transfer*. The link is therefore an operator assertion, and
these endpoints are how it gets made.

Spec: ``docs/spec-users-meta-2026-06-15.md`` §M1 (B2b's "the B4 seam") and
invariants 1, 2, 6, 7.

This module **calls** the seam; it never reimplements it. Every write goes
through :func:`roster_identity.link_external_id` /
:func:`roster_identity.unlink_external_id`, which own the semantics (appends
never replaces, links never creates, refuses ``pod.admins``, refuses a
conflicting holder without ``force``). What lives here is the HTTP shape and
the *honest presentation* of the refusals.

Endpoints (all under ``/api/admin/bots/<bot_id>/person-link``)::

  GET  /                → {ok, bot_id, ref, row_exists, reachable[],
                           linkable_channels[], one_id_per_channel: true}
  POST /link            → body {channel, external_id, force?}
                          200  {ok, channel, ids}
                          409  {ok: false, error: "conflict", conflicts[], …}
                          409  {ok: false, error: "channel_occupied", …}
                          400  {ok: false, error: "invalid"|"refused", …}
  POST /unlink          → body {channel, external_id} → {ok, removed}

What this is NOT
================

Linking is an **identity assertion only**. It does not admit anyone, does not
touch ``openclaw.json``, and deliberately does not run
``seed_identity.seed_channel_identity`` the way ``claim-primary`` does —
saying "these two ids are one human" must not, as a side effect, hand a new
platform id DM access. Admission stays the Users page's existing approve flow.

`force` is never the default
============================

``link_external_id`` raises :class:`~evolve_admin.roster_identity.PersonLinkError`
when another row already records the id. This module **never retries with
``force=True`` on its own**: it returns 409 with the conflicting rows named, and
the caller must send a second, explicit request carrying ``force: true``. The
409 body also states that forcing *appends* — the other row keeps its record —
because ``link_external_id`` appends and never moves.

SCOPE LIMIT — one id per channel, for now
=========================================

Both GET (which channels are offered) and POST (the enforcement) restrict
linking to a channel where the person has **no id yet** — the multi-platform
case (Discord + Telegram), not a second id on one channel.

Reason: bots run the plugin's ``roleResolver``, whose deployed build does
``String(val)`` over a channel's ``external_ids`` value. In JS a one-element
array stringifies without brackets (``String(["X"]) === "X"``), so one id per
channel is safe across the deploy window — but two ids on one channel yields
``"111,222"`` and breaks role resolution on the runtime authorization plane.
See ``_CHANNEL_OCCUPIED_HINT`` for the extension point.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from flask import Flask, jsonify, request

from .. import channel_registry as _channels
from .. import external_ids as _ext_ids
from .. import roster_identity as ri
from ..config import load_network, save_network


# ── The one-id-per-channel limit ─────────────────────────────────────────
#
# EXTENSION POINT (M1-B4a → unblocked by the plugin deploy): drop this guard
# and the `already_linked` filter in `_linkable_channels` once the new plugin
# build is deployed fleet-wide. The deployed `roleResolver` does `String(val)`
# on a channel's `external_ids` value; `String(["X"])` is `"X"` (safe) but
# `String(["111","222"])` is `"111,222"` — a bogus id that breaks role
# resolution on the runtime authorization plane. Until every bot runs the
# array-aware resolver, a second id on ONE channel is a live hazard, while a
# first id on a NEW channel is not. Nothing else about the seam changes:
# `link_external_id` already appends and is already idempotent.
_CHANNEL_OCCUPIED_HINT = (
    "This person already has an id on this channel. Adding a second id on the "
    "same channel is not offered yet — bots still resolve roles from a "
    "stringified id, where two ids on one channel become one bogus id and "
    "break role resolution. Linking a channel they have no id on (the "
    "multi-platform case) is safe and is what this affordance is for."
)


def _link_channel_specs() -> tuple[Any, ...]:
    """Channels a person can hold an admission key on (registry projection).

    Same predicate the roster↔OC coherence monitor reconciles on
    (``install is not None and supports_allowlist``) — deliberately, because
    that monitor's ``roster_oc_identity_unknown`` Signal is this surface's
    feed. A channel whose config can name an identity is a channel we may need
    to link an id on. Invariant 7: no channel literal lives here.
    """
    return _channels.where(
        lambda c: c.install is not None and c.supports_allowlist)


def _channel_view(spec: Any) -> dict:
    """UI-facing description of one linkable channel."""
    pairing = spec.pairing
    return {
        "channel": spec.id,
        "label": spec.display_label,
        "id_label": pairing.id_label if pairing else "External ID",
        "id_hint": pairing.id_format_hint if pairing else "",
    }


def person_label(network: Any, ref: ri.PersonRef) -> str:
    """Operator-facing name for a person row — used to NAME a collision.

    A conflict the operator has to judge is useless as ``primary_user:xyz``;
    they need "the row that already holds this id is <bot>'s owner". Falls
    back to the row's stable key when nothing better is recorded.
    """
    if ref.kind == ri.POD_ADMIN:
        return "Pod admins (the shared admin identity bag)"
    block = ri.row_block(network, ref) or {}
    name = block.get("name")
    bot_id = ref.bot_id or "?"
    if isinstance(name, str) and name.strip():
        return f"{name.strip()} — {bot_id}'s owner"
    return f"{bot_id}'s owner"


def _conflict_views(network: Any, refs: tuple) -> list[dict]:
    return [
        {
            "key": r.key,
            "kind": r.kind,
            "bot_id": r.bot_id,
            "label": person_label(network, r),
            "is_person": r.is_person,
        }
        for r in refs
    ]


def person_link_state(network: Any, bot_id: str) -> dict:
    """GET payload — where this person is reachable, and what can be linked.

    ``reachable`` is the row's ``external_ids`` rendered per channel; it is the
    multi-valued shape M1-B2 landed and that no surface rendered until now.
    ``linkable_channels`` applies the scope limit (channels with no id yet).
    """
    ref = ri.primary_user_ref(bot_id)
    block = ri.row_block(network, ref)
    specs = _link_channel_specs()
    known = {s.id: s for s in specs}

    reachable: list[dict] = []
    for channel, ids in _ext_ids.read_external_ids(block).items():
        spec = known.get(channel)
        reachable.append({
            "channel": channel,
            "label": spec.display_label if spec else channel,
            "ids": ids,
        })
    reachable.sort(key=lambda r: r["channel"])

    # The scope limit's read side — see _CHANNEL_OCCUPIED_HINT for the
    # extension point. Drop the `if not ids_for_row(...)` filter together with
    # the write-side guard in apply_link, never one without the other.
    linkable = [
        _channel_view(s) for s in specs
        if not ri.ids_for_row(network, ref, s.id)
    ]
    return {
        "ok": True,
        "bot_id": bot_id,
        "ref": ref.key,
        "row_exists": block is not None,
        "reachable": reachable,
        "linkable_channels": linkable,
        # Advertised so a client can explain the limit without re-deriving it.
        "one_id_per_channel": True,
        "one_id_per_channel_reason": _CHANNEL_OCCUPIED_HINT,
    }


def apply_link(
    network: dict,
    ref: ri.PersonRef,
    channel: Any,
    external_id: Any,
    *,
    force: bool = False,
) -> tuple[dict, int]:
    """Run the seam and translate its refusals into an HTTP-shaped result.

    Returns ``(payload, status)``. Mutates ``network`` on success only; the
    caller persists. Never sets ``force`` itself — it passes the caller's
    value through, so a forced append is always a second, deliberate request.
    """
    cleaned = str(channel).strip().lower() if isinstance(channel, str) else ""
    needle = str(external_id).strip() if external_id is not None else ""
    if not cleaned:
        return {"ok": False, "error": "invalid",
                "message": "channel is required"}, 400
    if not needle:
        return {"ok": False, "error": "invalid",
                "message": "external_id is required"}, 400

    # Scope limit — enforced here, not only in the UI, so a hand-rolled POST
    # cannot mint the "111,222" shape the deployed roleResolver mis-reads.
    # An id already recorded on THIS row is a no-op re-link, not a second id,
    # so it is allowed through to the (idempotent) seam.
    #
    # Gated on ``is_person`` deliberately: a row the seam refuses OUTRIGHT
    # (``pod.admins`` — appending there grants pod-admin) must surface THAT
    # refusal, not a channel-shaped one. "You may not link here at all"
    # outranks "not on this channel", so those refs fall straight through to
    # the seam below.
    existing = ri.ids_for_row(network, ref, cleaned) if ref.is_person else []
    if existing and needle not in existing:
        return {
            "ok": False,
            "error": "channel_occupied",
            "channel": cleaned,
            "existing_ids": list(existing),
            "message": _CHANNEL_OCCUPIED_HINT,
        }, 409

    # Compute the conflicting rows for presentation BEFORE calling the seam —
    # read-only, and it is what lets the 409 name the other person instead of
    # echoing a raw error string.
    conflicts = tuple(
        other for other in ri.rows_holding(network, cleaned, needle)
        if other != ref
    )

    try:
        ids = ri.link_external_id(network, ref, cleaned, needle, force=force)
    except ri.PersonLinkError as e:
        if conflicts and not force:
            return {
                "ok": False,
                "error": "conflict",
                "channel": cleaned,
                "external_id": needle,
                "conflicts": _conflict_views(network, conflicts),
                # Load-bearing: confirming APPENDS. The other row keeps its
                # record — link_external_id never moves an id — so the
                # operator is agreeing to "also this person", not "not that
                # person". Un-linking there is a separate explicit action.
                "appends_only": True,
                "message": str(e),
            }, 409
        # Anything else the seam refuses — a missing row, an unsupported row
        # kind, or the pod.admins refusal (appending there GRANTS pod-admin
        # rather than asserting sameness). Surfaced verbatim rather than
        # swallowed; the UI never offers pod.admins as a target, but if one
        # ever reaches here the operator sees why it was refused.
        return {"ok": False, "error": "refused", "message": str(e)}, 400

    return {
        "ok": True,
        "channel": cleaned,
        "external_id": needle,
        "ids": ids,
        "forced": bool(force and conflicts),
    }, 200


def apply_unlink(
    network: dict, ref: ri.PersonRef, channel: Any, external_id: Any,
) -> tuple[dict, int]:
    """Undo one link. Only ever touches ``ref``'s own row."""
    cleaned = str(channel).strip().lower() if isinstance(channel, str) else ""
    needle = str(external_id).strip() if external_id is not None else ""
    if not cleaned or not needle:
        return {"ok": False, "error": "invalid",
                "message": "channel and external_id are required"}, 400
    try:
        removed = ri.unlink_external_id(network, ref, cleaned, needle)
    except ri.PersonLinkError as e:
        return {"ok": False, "error": "refused", "message": str(e)}, 400
    return {"ok": True, "channel": cleaned, "external_id": needle,
            "removed": bool(removed)}, 200


def _bot_ref(network: Any, bot_id: str) -> Optional[ri.PersonRef]:
    bots = network.get("bots") if isinstance(network, dict) else None
    if not isinstance(bots, dict) or bot_id not in bots:
        return None
    return ri.primary_user_ref(bot_id)


def register_routes(app: Flask, network_path: Path) -> None:
    """Register the person-link endpoints on ``app``."""

    @app.get("/api/admin/bots/<bot_id>/person-link")
    def person_link_get(bot_id: str) -> Any:  # type: ignore[no-redef]
        net = load_network(network_path)
        if _bot_ref(net, bot_id) is None:
            return jsonify({"ok": False, "error": "unknown_bot",
                            "message": f"no such bot: {bot_id}"}), 404
        return jsonify(person_link_state(net, bot_id))

    @app.post("/api/admin/bots/<bot_id>/person-link/link")
    def person_link_post(bot_id: str) -> Any:  # type: ignore[no-redef]
        body = request.get_json(silent=True) or {}
        net = load_network(network_path)
        ref = _bot_ref(net, bot_id)
        if ref is None:
            return jsonify({"ok": False, "error": "unknown_bot",
                            "message": f"no such bot: {bot_id}"}), 404
        payload, status = apply_link(
            net, ref, body.get("channel"), body.get("external_id"),
            # Passed straight through. The UI only sets this after the
            # operator confirms a named 409 conflict.
            force=bool(body.get("force", False)),
        )
        if status == 200:
            save_network(net, network_path)
        return jsonify(payload), status

    @app.post("/api/admin/bots/<bot_id>/person-link/unlink")
    def person_unlink_post(bot_id: str) -> Any:  # type: ignore[no-redef]
        body = request.get_json(silent=True) or {}
        net = load_network(network_path)
        ref = _bot_ref(net, bot_id)
        if ref is None:
            return jsonify({"ok": False, "error": "unknown_bot",
                            "message": f"no such bot: {bot_id}"}), 404
        payload, status = apply_unlink(
            net, ref, body.get("channel"), body.get("external_id"))
        if status == 200 and payload.get("removed"):
            save_network(net, network_path)
        return jsonify(payload), status
