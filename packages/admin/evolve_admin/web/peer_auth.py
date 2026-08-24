"""evolve_admin.web.peer_auth — decorators for unix-socket peer auth.

Phase E.3 (internal/spec-evo-account-separation-2026-05-25.md) introduces a
unix-socket binding alongside the admin daemon's TCP loopback. The admin
UI continues to use the TCP binding; evo's tool runtime uses the unix
socket so the admin daemon can authenticate it by peer-credential uid.

This module's ``@require_trusted_peer`` decorator gates a route to:

  1. Requests that arrived over the unix socket (not TCP), AND
  2. Whose peer effective uid matches one of an allowed list of macOS
     users (default: ``evo``; pre-separation fallback: ``evolve``).

The evo account separation (Phase E.2.b cutover) shipped 2026-05-30 —
a cut-over pod runs evo's gateway as the dedicated ``evo`` user. The
dual-trust migration window this module carried (``evolve`` + ``evo``)
was decommissioned 2026-06-10 (roadmap 2.11): on a cut-over pod,
trusting ``evolve`` meant the admin daemon's own uid — and anything
that ever ran as it — kept member-tier socket access for no reason.

The separation signal is the gateway's *actual runtime user*,
``network.json::bots.<primary>.user``, which the operator-run
``migrate-evo-account-cutover`` flips to ``"evo"`` atomically with the
gateway-plist rewrite. Account existence is deliberately NOT the
signal: the setup wizard provisions the ``evo`` account on every new
install while the gateway still runs as ``evolve`` until the operator
cuts over — keying trust on the account would lock those pods' evo
tools out. Resolution happens per request, so the cutover flips trust
without a daemon restart.

The decorator never trusts TCP requests. If a route is decorated with
``@require_trusted_peer`` and the admin UI hits it over TCP, the request
is rejected with 403. This is intentional: TCP is unauthenticated
loopback and the protected routes carry cross-bot read/write surface
that must not be reachable from a browser session.
"""
from __future__ import annotations

import functools
import logging
import os
import pwd
from pathlib import Path
from typing import Callable

from flask import abort, request

log = logging.getLogger(__name__)


# Default trusted user name for the evo↔admin-daemon channel.
# On a cut-over pod (E.2.b shipped 2026-05-30) evo's gateway runs as
# the dedicated ``evo`` user — that is the only peer these routes serve.
DEFAULT_TRUSTED_USERS: tuple[str, ...] = ("evo",)

# Pods not yet cut over (including FRESH INSTALLS — the wizard
# provisions the ``evo`` account at setup but the gateway runs as
# ``evolve`` until the operator runs ``migrate-evo-account-cutover``)
# still run evo's gateway as the ``evolve`` user. The fallback applies
# exactly while network.json says the gateway runs as ``evolve``.
PRE_SEPARATION_FALLBACK_USERS: tuple[str, ...] = ("evolve",)


def _evo_gateway_user(network_path: "Path | None" = None) -> "str | None":
    """The macOS user evo's gateway runs as, per network.json — or None.

    Reads ``bots.<primary>.user``. The E.2.b cutover
    (``migrate-evo-account-cutover``) flips this field to ``"evo"``
    atomically with the gateway-plist rewrite
    (``setup_wizard._evo_cutover_update_network``), so it tracks the
    actual runtime user. The wizard pins it to ``"evolve"`` on fresh
    installs (``primary_entry["user"] = "evolve"`` — account ≠ bot id).

    Returns None when undeterminable: network.json absent/unreadable,
    no primary bot resolvable, or no ``user`` field on the entry.
    """
    try:
        from ..config import DEFAULT_NETWORK_CONFIG, load_network
        net = load_network(network_path or DEFAULT_NETWORK_CONFIG)
    except Exception:  # noqa: BLE001 — absent/unreadable network.json
        return None
    _bots = net.get("bots")
    bots: dict = _bots if isinstance(_bots, dict) else {}
    # Primary-bot resolution, mirroring analyzer/primary_bot.py:
    # explicit ``primary`` field → any bot with role=="primary" →
    # legacy fallback to a bot literally named "evolve".
    _primary = net.get("primary")
    primary: "str | None" = _primary if isinstance(_primary, str) and _primary else None
    if primary is None:
        for bid, cfg in bots.items():
            if isinstance(cfg, dict) and cfg.get("role") == "primary":
                primary = bid
                break
        if primary is None and "evolve" in bots:
            primary = "evolve"
    if not primary:
        return None
    entry = bots.get(primary)
    user = entry.get("user") if isinstance(entry, dict) else None
    if isinstance(user, str) and user:
        return user
    # No explicit ``user`` → the bot runs under an account named after
    # itself (``primary_bot.primary_bot_user`` semantics, mirrored so a
    # legacy pod whose primary is literally named "evolve" keeps
    # trusting the evolve uid even after E.2.a provisions the unused
    # ``evo`` account).
    return primary


def _default_trusted_users(network_path: "Path | None" = None) -> tuple[str, ...]:
    """The trust list for routes that don't pass explicit names.

    ``("evo",)`` when network.json says evo's gateway runs as ``evo``
    (cut-over pod); ``("evolve",)`` when it runs as anything else
    (pre-cutover pod, fresh installs included). Never both — the
    dual-trust migration window closed 2026-06-10 (roadmap 2.11).

    When the gateway user is undeterminable (no/unreadable network.json
    or no primary bot — a pod too broken for the daemon to serve
    anyway), tiebreak on whether the ``evo`` account exists.
    """
    user = _evo_gateway_user(network_path)
    if user == "evo":
        return DEFAULT_TRUSTED_USERS
    if user is not None:
        return PRE_SEPARATION_FALLBACK_USERS
    try:
        pwd.getpwnam("evo")
    except KeyError:
        return PRE_SEPARATION_FALLBACK_USERS
    return DEFAULT_TRUSTED_USERS


def _resolve_uids(usernames: tuple[str, ...]) -> set[int]:
    """Look up macOS uids for a list of user names; skip unknowns.

    Returns the set of resolved uids. Names that don't exist on this
    system (e.g. ``evo`` before Phase E.2.a has run, or in a test
    environment) are silently dropped — the auth check then matches
    against the remaining names. This is the right behavior: if ``evo``
    doesn't exist yet, only ``evolve`` is trusted, which is correct
    pre-cutover.
    """
    uids: set[int] = set()
    for name in usernames:
        try:
            entry = pwd.getpwnam(name)
        except KeyError:
            continue
        uids.add(entry.pw_uid)
    return uids


def _request_transport() -> str:
    """The transport the current Flask request arrived over.

    Unix socket binding sets environ["REMOTE_TRANSPORT"] = "unix-socket".
    The TCP binding doesn't set anything, so absence implies TCP.
    """
    return request.environ.get("REMOTE_TRANSPORT") or "tcp"


def _request_peer_uid() -> int:
    """Peer effective uid for the current request.

    Returns -1 for TCP requests (no peer credentials available) and for
    unix-socket requests where getpeereid extraction failed.
    """
    return int(request.environ.get("REMOTE_PEER_UID", -1))


# ── Per-bot peer-uid identity (bot-facing Google tool routes) ─────────────────


def _bot_uid_map(network_path: "Path | None" = None) -> dict[int, str]:
    """Build a ``peer-uid → bot_id`` map from network.json.

    Each member bot runs as its own macOS user (``bots.<id>.user`` — which
    may differ from the bot id; see ``get_bot_user``). The kernel reports
    the connecting process's effective uid on the unix socket, so mapping
    that uid back to a bot id binds the caller's identity *below the LLM*
    — a bot literally cannot present another bot's uid.

    Skips bots whose account can't be resolved (planned bots, accounts not
    yet provisioned). **Ambiguous uids fail closed:** if two bot ids resolve
    to the same macOS account (a shared account — ``bot_id ≠ account name``),
    that uid is DROPPED from the map rather than bound to one of them. The
    peer uid can't uniquely identify the caller, so binding it to either bot
    would risk acting as the wrong bot — the route then 403s for those bots
    (they can use the operator bridge instead). Returns an empty map when
    network.json is unreadable (fail-closed: no caller resolves → 403).
    """
    import pwd

    try:
        from ..config import DEFAULT_NETWORK_CONFIG, get_bot_user, load_network
        net = load_network(network_path or DEFAULT_NETWORK_CONFIG)
    except Exception:  # noqa: BLE001 — absent/unreadable network.json
        return {}
    bots = net.get("bots")
    if not isinstance(bots, dict):
        return {}
    uid_to_bots: dict[int, list[str]] = {}
    for bot_id, cfg in bots.items():
        if not isinstance(cfg, dict):
            continue
        try:
            user = get_bot_user(bot_id, net)
            uid = pwd.getpwnam(user).pw_uid
        except Exception:  # noqa: BLE001 — unprovisioned/absent account: skip
            continue
        uid_to_bots.setdefault(uid, []).append(bot_id)
    # Only uids mapping to EXACTLY ONE bot are bindable — drop ambiguous ones.
    return {uid: ids[0] for uid, ids in uid_to_bots.items() if len(ids) == 1}


def resolve_peer_bot_id(network_path: "Path | None" = None) -> "str | None":
    """Return the bot id that owns the current request's unix-socket peer.

    The server-trusted identity for the bot-facing ``/api/google/*`` routes.
    Returns None — and the caller must 403 — unless ALL hold:
      1. the request arrived over the unix socket (TCP has no peer creds),
      2. a peer effective uid was extracted, and
      3. that uid maps to a bot in network.json.

    NEVER reads a bot/subject from the request body or query — identity is
    bound purely from the kernel-reported peer uid. This is the fix for the
    bridge's client-supplied-``bot``-arg cross-bot data-leak hole
    (spec §1.2 / §7).
    """
    if _request_transport() != "unix-socket":
        return None
    peer_uid = _request_peer_uid()
    if peer_uid < 0:
        return None
    return _bot_uid_map(network_path).get(peer_uid)


# Bot-facing route prefixes whose identity is the unix-socket peer uid (the route binds
# the caller to the bot the uid maps to, then enforces its own gating). The device-auth
# before_request gate exempts these for a recognized bot peer. Each is a deliberately
# narrow prefix so the exemption never widens access to any other admin surface:
#   /api/google/    — curated Google tools          (google_bot_routes)
#   /api/directory/ — user-directory read tool + digest (directory_bot_routes)
# The trailing slash makes the ``startswith`` boundary-safe (``/api/googlex`` /
# ``/api/google-private`` don't match).
_PEER_BOT_ROUTE_PREFIXES: tuple[str, ...] = ("/api/google/", "/api/directory/")

# Exact bot-facing tool paths (not prefixes) the gate exempts for a recognized bot
# peer. Used where a whole ``/api/<x>/`` subtree must NOT be exempted because it
# also carries admin-UI-only routes:
#   /api/applications/expand — app capability index expand_app (applications_bot_routes).
# The ``/api/applications/`` subtree also holds admin-only lifecycle routes
# (``/api/applications/<bot>/<app>/pause`` etc.), so a prefix exemption would
# over-grant; only the exact expand_app path is bot-facing + peer-uid-bound.
_PEER_BOT_ROUTE_EXACT: frozenset[str] = frozenset({
    "/api/applications/expand",
})

# Member-bot RPC routes the gateway plugin reaches over the socket — the second
# family ``peer_bot_route_exempt`` recognizes. These are the endpoints
# TurnObserver's EvoDispatchClient + BetterEngineClient call (instantiated in
# EVERY gateway, incl. member bots), so once the plugin transport moves onto the
# socket a member bot's peer-uid request must clear the device-cookie gate.
#
# SCOPE IS DELIBERATELY NARROW — only routes a MEMBER-bot gateway actually
# calls. Primary-only surfaces (``/api/primary/state/*``, ``/api/evo/help/*``,
# ``/api/evo/intake*`` — gated behind ``role === "primary"`` in the plugin,
# index.ts:166) are EXCLUDED: they reach the socket only as the trusted evo
# peer, already covered by the trusted-peer branch, so exempting them here would
# over-grant. Likewise ``/api/better/recommendations`` is matched as a SUBTREE
# (bare list + /top + /<id>/accept|reject|snooze) but ``pending-admin-tasks`` as
# an EXACT path so its ``/<id>/resolve`` sub-resource (admin-UI only) stays
# gated.
#
# Matching is BOUNDARY-SAFE (``path == base`` or ``path.startswith(base + "/")``)
# so an exact base like ``/api/evo/dispatch`` can't also match a sibling such as
# ``/api/evo/dispatch-secret``.
_MEMBER_BOT_RPC_PATHS_EXACT: frozenset[str] = frozenset({
    "/api/evo/dispatch",                 # EvoDispatchClient.dispatch
    "/api/evo/wizard/active",            # EvoDispatchClient.findActiveWizard
    "/api/evo/wizard/turn",              # EvoDispatchClient.wizardTurn
    "/api/better/pending-admin-tasks",   # BetterEngineClient.addPendingAdminTask (POST) + list (GET)
})
_MEMBER_BOT_RPC_SUBTREES: tuple[str, ...] = (
    "/api/better/recommendations",       # BetterEngineClient: list + /top + /<id>/accept|reject|snooze
)


def _is_member_bot_rpc_path(path: str) -> bool:
    """Boundary-safe match of ``path`` against the member-bot RPC route set."""
    if path in _MEMBER_BOT_RPC_PATHS_EXACT:
        return True
    return any(
        path == base or path.startswith(base + "/")
        for base in _MEMBER_BOT_RPC_SUBTREES
    )


def peer_bot_route_exempt(network_path: "Path | None" = None) -> bool:
    """True iff this request is a bot calling one of its own peer-uid-bound routes.

    The device-auth ``before_request`` gate calls this so a member bot's unix-socket
    request reaches a bot-facing route — the per-bot tool routes (``/api/google/*``,
    ``/api/directory/*``, ``/api/applications/expand``) or the member-bot RPC routes
    (evo dispatch, evo wizard, the Better recommendations channel) — which then bind
    identity via ``resolve_peer_bot_id`` and enforce their own gating. Scoped to
    :data:`_PEER_BOT_ROUTE_PREFIXES` + :data:`_PEER_BOT_ROUTE_EXACT` +
    :func:`_is_member_bot_rpc_path` so the exemption never widens access to any other
    surface; a non-bot peer (or TCP) resolves to None and is NOT exempt.

    NOTE: this only bypasses the DEVICE-COOKIE gate for a recognized member-bot peer-uid.
    Routes that trust a body/query ``bot_id`` for anything authoritative must additionally
    bind it via :func:`member_peer_bot_id` so a member bot can act only AS ITSELF.
    """
    if not (
        request.path.startswith(_PEER_BOT_ROUTE_PREFIXES)
        or request.path in _PEER_BOT_ROUTE_EXACT
        or _is_member_bot_rpc_path(request.path)
    ):
        return False
    return resolve_peer_bot_id(network_path) is not None


def _daemon_uid() -> int:
    """The admin daemon's own effective uid (the ``evolve`` service user).

    Seam so tests that simulate a *member-bot* peer with the test runner's
    own uid (the technique in test_member_bot_socket_auth.py — the real
    resolver must map the uid to a bot) can pin the daemon's uid to a
    different sentinel; in the test process both are otherwise the same.
    Effective uid, to match what peer credentials report for the peer.
    """
    return os.geteuid()


def device_gate_trusted_peer() -> bool:
    """True iff the current request's socket peer-uid bypasses the
    device-cookie gate (``server._enforce_device_auth``).

    Two peers qualify:
      1. The trusted set (``evo`` — same check as ``require_trusted_peer``'s
         default on a cut-over pod), so evo's tool runtime reaches the
         daemon without a browser pairing.
      2. The daemon's OWN uid (the ``evolve`` service user). A local
         process already running as that user can read the daemon's token
         store and every file it serves, so trusting its socket peer-uid
         widens nothing. This is what admits same-user local tooling —
         the Gate-2 gallery-verify harness runs as ``evolve`` and 401ed on
         cut-over pods (trust list is evo-only) before this exemption.

    Any other uid — and any TCP request — returns False and falls through
    to the cookie gate, so the exemption never widens access beyond evo +
    the daemon's own user. NOT for route-level auth: ``require_trusted_peer``
    (deliberately narrower — no self-uid) stays the guard for the member-tier
    socket routes.
    """
    if _request_transport() != "unix-socket":
        return False
    peer_uid = _request_peer_uid()
    if peer_uid < 0:
        return False
    return peer_uid == _daemon_uid() or peer_uid in _resolve_uids(
        DEFAULT_TRUSTED_USERS
    )


def member_peer_bot_id(network_path: "Path | None" = None) -> "str | None":
    """The bot id a NON-trusted member-bot socket peer is bound to, or None.

    The authority-binding primitive for the member-bot RPC routes. Returns the
    peer-uid→bot_id binding (``resolve_peer_bot_id``) ONLY when the caller is a
    member-bot peer that is NOT in the trusted set (evo). Returns None for:
      - TCP / device-authed callers (the admin UI legitimately acts on ANY bot),
      - the trusted evo peer (the central dispatcher fans out to every bot — it
        bypasses the device gate via the trusted-peer branch and never reaches
        the routes that call this, but we return None defensively so an evo call
        is never constrained), and
      - any socket peer whose uid maps to no bot.

    A route that trusts a body/query ``bot_id`` for anything authoritative calls
    this and, when it returns non-None, MUST constrain the action to that bot id:
    a member bot may act only AS ITSELF. The id is bound from the kernel-reported
    peer uid, so a member bot cannot present another bot's id (same guarantee as
    ``resolve_peer_bot_id`` for the Google/directory routes).
    """
    if _request_transport() != "unix-socket":
        return None
    peer_uid = _request_peer_uid()
    if peer_uid < 0:
        return None
    # Trusted peers (evo) act on behalf of any bot — never constrained here.
    if peer_uid in _resolve_uids(_default_trusted_users(network_path)):
        return None
    return _bot_uid_map(network_path).get(peer_uid)


def require_trusted_peer(
    *trusted_user_names: str,
    network_path: "Path | None" = None,
) -> Callable[[Callable], Callable]:
    """Decorator: 403 unless the request came from a trusted peer.

    Two stacked checks:
      1. Transport must be ``unix-socket``. TCP requests are rejected
         outright — TCP carries the admin UI surface and has no peer
         credentials to validate.
      2. Peer effective uid must match one of the trusted users
         resolved by ``pwd.getpwnam``. When no explicit names are
         passed, the trust list tracks the gateway's actual runtime
         user from network.json: ``("evo",)`` on cut-over pods,
         ``("evolve",)`` before the cutover (see
         ``_default_trusted_users``). Pass ``network_path`` so the
         resolution reads the same network.json the daemon serves.

    Usage:

        @app.route("/api/admin/bot/<bot_id>/openclaw-config")
        @require_trusted_peer()
        def admin_bot_openclaw_config(bot_id):
            ...

    With explicit names:

        @require_trusted_peer("evolve", "evo")
        def handler(...):
            ...

    The trust resolution happens at request time, not decoration time.
    So if ``evo`` doesn't exist when the daemon starts but is created
    later (Phase E.2.a provisioning), the decorator picks up the new
    uid on the next request without a daemon restart.
    """
    def _decorator(view_func: Callable) -> Callable:
        @functools.wraps(view_func)
        def _wrapper(*args, **kwargs):
            transport = _request_transport()
            if transport != "unix-socket":
                log.warning(
                    "peer_auth: rejected %s %s (transport=%s)",
                    request.method, request.path, transport,
                )
                abort(
                    403,
                    description=(
                        "this endpoint is reachable only via the "
                        "admin-daemon unix socket"
                    ),
                )

            # Resolved per request (not at decoration time) so the
            # default trust list tracks the cutover state live.
            names = trusted_user_names or _default_trusted_users(network_path)
            trusted_uids = _resolve_uids(names)
            peer_uid = _request_peer_uid()
            if peer_uid < 0:
                log.warning(
                    "peer_auth: rejected %s %s (no peer uid)",
                    request.method, request.path,
                )
                abort(403, description="peer credentials unavailable")
            if peer_uid not in trusted_uids:
                log.warning(
                    "peer_auth: rejected %s %s (peer uid %d not in %s)",
                    request.method, request.path, peer_uid, trusted_uids,
                )
                abort(
                    403,
                    description=(
                        f"peer uid {peer_uid} not in trusted set"
                    ),
                )

            return view_func(*args, **kwargs)

        return _wrapper

    return _decorator
