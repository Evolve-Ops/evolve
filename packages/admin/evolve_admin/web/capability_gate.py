"""Pod-scoped capability gate — the shared home for ``check_pod_admin``.

``routes_bot_users._check_capability`` resolves a role **on a bot**: it needs a
``bot_id`` for the overlay load and for ``roster_overlay.resolve_role``. Some
privileged writes have no bot to scope to — they mutate ``pod.*`` directly and
take effect pod-WIDE. Those need a pod-scoped analogue, and this module is it.

Why a module of its own rather than a private in one route file: the gate now
has more than one consumer, and they sit in different layers.

CALL SITES
----------

``routes_identity.py`` — ``claim-admin`` / ``revoke-admin`` (#3647). Writes
``pod.admins.external_ids`` directly.

``server.py::api_config`` — the second door onto the same write, and the
reason this module exists:

  * ``action="podAdmins"`` — ``{"op":"add","channel":…,"external_id":…}``
    reaches ``external_ids.add_external_id`` → ``pod["admins"]`` →
    ``save_network``: byte-for-byte the pod-wide promotion ``claim-admin``
    performs. ``op="remove"`` is the inverse (lockout / DoS primitive).
  * ``action="pod"`` — writes ``pod.admin_passphrase`` /
    ``pod.primary_passphrase``, a ONE-HOP PIVOT to the same escalation: set
    ``pod.admin_passphrase`` and ``evo.identity.matches_admin_passphrase``
    returns True, so ``evo claim <phrase>`` over DM reaches
    ``evo/dispatch.py``'s IN-PROCESS ``claim_admin``, which has no capability
    check of its own. The branch's third field, ``ssh_target``, is gated with
    them rather than left as a third ungated door on a gated branch: it rides
    the same request from the same sole caller (``settings.js``
    ``savePodIdentity`` sends all three together, so a field-selective gate
    would buy no compatibility), and while it grants no capability it IS
    operator-facing — ``config.resolve_pod_context`` interpolates it into the
    copy-paste ``ssh …`` remediation strings that alerts, health hints and the
    puller's recovery recipe show the operator.

Why this matters pod-wide rather than per-bot: ``roster_overlay.resolve_role``
checks ``stable_id in _pod_admin_ids_for(network, platform)`` INDEPENDENT of
``bot_id``, and ``DEFAULT_ROLE_CAPABILITIES["admin"]`` is ``["*"]``. One
pod-admin claim therefore resolves to ``admin`` — every capability — on every
bot, re-opening every gate #3647 / #3643 / #3642 install.

The other ``api_config`` action branches (``alerts``, ``appTesting``,
``backup``, ``heal``, ``classifiers``, ``security``, ``timezone``) are
deliberately NOT gated: they are not identity-escalation surfaces, and a
handler-level gate over all of them was rejected without a full per-action
caller audit because it could brick the setup wizard / install flow.

#3647 landed the helper as a module-local private in ``routes_identity.py`` and
its PR body called for exactly this move once a second consumer appeared. The
alternative — ``server.py`` reaching into ``routes_identity``'s privates — would
have left an odd dependency direction (server → a route module) for a rule that
belongs to neither. Keeping ONE definition is the point: a pod-scoped gate that
drifts between two copies is worse than either copy alone, because the looser
copy silently becomes the pod's real policy.

Deliberately NOT a new ``pod.admins.mutate`` built-in capability: adding a key
to ``capabilities.BUILTIN_CAPABILITIES`` widens the ``"*"`` expansion in
``resolve_role_capabilities``, silently granting the new permission to any role
an operator has bound to ``"*"`` in a per-bot ``role_bindings`` overlay. That
would be a capability-semantics change; this is containment.
"""

from __future__ import annotations

from flask import jsonify

from ..evo.identity import is_admin
from .. import roster_overlay as ro
# Same import-the-gate precedent routes_directory follows for the per-bot gate:
# the header primitives live in routes_bot_users and are reused verbatim rather
# than re-implemented. Nothing here modifies them — the header semantics
# (absent ⇒ trusted only for the authenticated admin UI, malformed ⇒ denied on
# every transport) are settled by #3435 / WO-H1-2 and inherited as-is.
from .routes_bot_users import (
    _MALFORMED_IDENTITY,
    _is_authenticated_ui_request,
    _parse_requester_identity,
    _shared_dir_from_net,
)

__all__ = ["check_pod_admin", "pod_admin_denial"]


def pod_admin_denial(net: dict, action: str):
    """``check_pod_admin`` wrapped as a ready-to-return 403, or ``None``.

    Call sites read ``if (denied := pod_admin_denial(net, "...")): return
    denied`` — two lines, no tuple unpacking, and the 403 body shape
    (``{"error": "forbidden", "detail": reason}``) is identical everywhere by
    construction rather than by three handlers agreeing to spell it the same.
    """
    ok, reason = check_pod_admin(net, action)
    if ok:
        return None
    return jsonify({"error": "forbidden", "detail": reason}), 403


def check_pod_admin(net: dict, action: str) -> "tuple[bool, str | None]":
    """Pod-scoped analogue of ``routes_bot_users._check_capability``.

    Reuses the settled *transport* semantics verbatim (absent header ⇒ trusted
    only for the authenticated admin UI; malformed ⇒ denied on every transport)
    and substitutes the one role rule that is meaningful pod-wide: **only an
    existing pod admin may perform a pod-admin-tier write.**

    That is a restriction of existing semantics, not a new tier.
    ``roster_overlay.resolve_role`` already returns ``"admin"`` for exactly this
    predicate — ``stable_id in _pod_admin_ids_for(net, platform)``,
    bot-independent — and ``"admin"`` maps to ``["*"]`` in
    ``capabilities.DEFAULT_ROLE_CAPABILITIES``. Any caller this helper admits
    would already resolve to ``admin`` on every bot.

    One precision note on that claim, since it is the gate's whole
    justification: ``admin`` maps to ``["*"]`` **by default**, but
    ``resolve_role_capabilities`` lets a per-bot ``role_bindings`` overlay
    NARROW a role (only ``"blocked"`` is override-proof). So an operator who
    hand-binds ``admin`` to a short list on one bot would have a pod admin whom
    the per-bot gate refuses there while this one still admits them pod-wide —
    i.e. this is not strictly a subset of ``_check_capability``. Unreachable
    today (nothing in the repo writes ``role_bindings``; it is hand-edit-only
    until the bindings UI ships), but whoever ships that UI should revisit
    whether pod-scoped writes need a per-bot narrowing check too.

    The sticky block is honored explicitly to keep that last sentence TRUE.
    ``resolve_role`` checks ``is_blocked`` BEFORE the pod-admin branch, so a
    blocked identity resolves to ``"blocked"`` (no capabilities) on that bot.
    There is no single overlay at pod scope, so this checks every bot's overlay
    and denies an identity blocked on ANY of them. Without it the pod-scoped
    gate would be strictly LOOSER than the per-bot one, and the operator's block
    would be trivially escapable: a blocked pod admin, refused by the per-bot
    gate, could still mint a second, unblocked pod-admin id and walk straight
    back in on every bot.

    Bootstrapping a fresh pod is unaffected. The admin UI reaches these routes
    but sends no header, so it is trusted by the same rule ``_check_capability``
    applies; and the two real first-admin paths (``evo.dispatch``'s DM
    ``evo claim <passphrase>`` flow and ``evo.wizard.engine``'s setup-wizard
    challenge) call ``evo.identity.claim_admin`` in-process behind the pod admin
    passphrase and never touch HTTP at all.

    ``action`` is a short human phrase naming the attempted write; it appears
    verbatim in the 403 ``detail`` so an operator reading a denial log can tell
    which door was tried.
    """
    req = _parse_requester_identity()
    if req is _MALFORMED_IDENTITY:
        # Header PRESENT but unparseable — denied on EVERY transport, so an
        # asserted-but-broken identity can never fall through to trusted-UI.
        return False, (
            f"malformed X-Requester-Identity header: pod-scoped {action} "
            f"asserted an identity that does not parse as "
            f"<platform>:<stable_id>"
        )
    if not req:
        if _is_authenticated_ui_request(net):
            return True, None  # authenticated admin UI — trusted client
        return False, (
            f"no requester identity: pod-scoped {action} reached without an "
            f"X-Requester-Identity header over a non-UI transport (unix "
            f"socket or unauthenticated caller)"
        )
    platform, stable_id, _ = req
    # Sticky deny FIRST — same precedence resolve_role uses (blocked beats the
    # pod-admin claim).
    #
    # Precisely what this does and does NOT guarantee: a READABLE overlay
    # carrying a block is decisive. An overlay that will not load is treated as
    # "no block", NOT as a denial — ``roster_overlay.load_overlay`` swallows
    # FileNotFoundError / PermissionError / OSError / JSONDecodeError and
    # returns the empty-overlay shape, so an unreadable or corrupt overlay is
    # indistinguishable here from an absent one, and the ``except`` below never
    # sees it. A blocked pod admin who can make every blocking overlay
    # unreadable therefore still passes this check. That exposure is inherited
    # deliberately — it is exactly ``_check_capability``'s own behavior, and
    # "absent overlay" is the normal state on a fresh pod, so failing closed on
    # it would brick every pod that has never blocked anyone. Tightening it
    # needs a load_overlay variant that distinguishes absent from unreadable,
    # which is a roster-store change and not this gate's to make.
    try:
        shared = _shared_dir_from_net(net)
        for bot_id in (net.get("bots") or {}):
            if ro.is_blocked(ro.load_overlay(shared, bot_id),
                             platform, stable_id):
                return False, (
                    f"requester {platform}:{stable_id} is blocked on bot "
                    f"{bot_id!r}; a blocked identity may not {action} "
                    f"(block is sticky and pod-wide for this action)"
                )
    except Exception:  # noqa: BLE001 — never let overlay IO open the gate
        return False, (
            f"could not verify block state for {platform}:{stable_id}; "
            f"{action} denied"
        )
    # Tolerant reader under the hood (``external_ids.has_id``) — the legacy
    # SCALAR shape is honored, so this is membership, not substring matching.
    if is_admin(net, platform, stable_id):
        return True, None
    return False, (
        f"requester {platform}:{stable_id} is not a pod admin; {action} "
        f"requires the pod-admin tier"
    )
