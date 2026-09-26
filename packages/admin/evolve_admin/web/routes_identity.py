"""Admin identity routes — claim primary/admin via the UI rather than DM.

Phase 5 of the alerts hardening: security_warden flags multi-user bots
without a primary/admin claim. The existing path was for the operator to
DM ``evo claim charles`` / ``evo claim darwin`` to the bot. This module
provides a server-side path so the alerts UI can offer a one-form fix.

POST endpoints:

  POST /api/admin/identity/claim-primary
       body: {bot_id, channel, external_id, pod_user?, name?, force?}
       returns: {ok: true, primary_user: {...}}

  POST /api/admin/identity/claim-admin
       body: {channel, external_id, pod_user?, name?}
       returns: {ok: true, admins: {...}}

  POST /api/admin/identity/revoke-admin            (1326 follow-up)
       body: {channel, external_id, drop_pod_user?}
       returns: {ok: true, removed: bool}

  POST /api/admin/identity/set-pod-passphrase      (1326 follow-up)
       body: {kind: "admin"|"primary", passphrase}
       returns: {ok: true}

  POST /api/admin/identity/set-bot-passphrase      (1326 follow-up)
       body: {bot_id, passphrase}   (null/empty passphrase clears override)
       returns: {ok: true}

  POST /api/admin/identity/resolve-name            (name_resolver hookup)
       body: {channel, external_id, refresh?: bool}
       returns: {ok: true, resolved: {username, name, cached_at}}
                or  {ok: false, reason: "<why>"} when lookup fails.

  POST /api/admin/identity/discover-primary        (auto-discovery)
       body: {bot_id, lookback_days?: int, top_k?: int}
       returns: {ok: true, candidates: [
                  {channel, external_id, turn_count, last_seen,
                   first_seen, username?, display_name?}, ...
                ]}
                Empty list when no human turn history exists.

GET that aggregates the current claim state across all known bots
(the UI's "what's missing right now" view):

  GET /api/admin/identity
       returns: {
         pod_context: {
           admin_user: <Unix-account>,
           hostname:   <socket.gethostname>,
           admin_base_url: <resolve_admin_base_url>,
         },
         pod_admins: {external_ids, pod_users, names, resolved_names},
         pod_passphrases: {admin, primary},
         bots: {<id>: {
           multi_user, role, primary_user (incl. optional name +
             resolved_names cache), primary_passphrase_override,
             missing: [...]
         }},
         members: [...],
       }

The endpoints are mounted under ``/api/admin/`` to match the
remediation routes; auth is presumed (admin UI gates at the network/
session layer).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request

from ..config import load_network, resolve_admin_base_url, save_network
from .. import external_ids as _external_ids
from ..evo import identity_discovery, name_resolver
from ..evo.identity import (
    ClaimError,
    admin_passphrase,
    bot_primary_passphrase_override,
    claim_admin,
    claim_primary,
    clear_primary,
    primary_passphrase,
    revoke_admin,
    set_bot_primary_passphrase,
    set_pod_admin_passphrase,
    set_pod_primary_passphrase,
)
# Same import-the-gate precedent routes_directory follows: the capability
# check and the header primitives live in routes_bot_users and are reused
# verbatim here rather than re-implemented. Nothing in this module modifies
# them — the header semantics (absent ⇒ trusted only for the authenticated
# admin UI, malformed ⇒ denied on every transport) are settled by #3435 /
# WO-H1-2 and inherited as-is.
from .routes_bot_users import _check_capability
# The pod-scoped gate. It began life as a module-local private here; a second
# consumer (server.py::api_config, which reaches the SAME pod.admins write via
# action="podAdmins") moved it to its own module so there is exactly ONE
# definition. Re-bound to the old private name so this module's call sites and
# tests read unchanged.
from .capability_gate import check_pod_admin as _check_pod_admin


# The bot-scoped identity mutations (primary_user, plus the allowFrom seed
# that rides along with a primary claim) are roster writes — same floor the
# overlay/block/ignore mutations in routes_bot_users already enforce.
_IDENTITY_WRITE_CAPABILITY = "bot.roster.mutate"


def register_routes(app: Flask, network_path: Path) -> None:
    """Register identity-claim endpoints on ``app``."""

    @app.get("/api/admin/identity")
    def identity_overview() -> Any:  # type: ignore[no-redef]
        import socket as _socket

        net = load_network(network_path)
        bots = net.get("bots", {}) or {}
        pod = net.get("pod", {}) or {}
        admins = pod.get("admins", {}) or {}
        # Normalized: empty channels are already dropped, so a non-empty
        # map IS "an admin is recorded" (M1-B2).
        admin_external = _external_ids.read_external_ids(admins)
        has_admin = bool(admin_external)

        # Operator-facing pod context — the Unix admin, the machine
        # hostname, and the admin URL. Surfaced read-only at the top of
        # the Identity subtab so the operator sees all four identity
        # concepts (Unix admin, machine, messaging admins, per-bot
        # primaries) in one place and doesn't conflate them.
        try:
            hostname = _socket.gethostname().split(".")[0] or "(unknown)"
        except Exception:  # noqa: BLE001
            hostname = "(unknown)"
        pod_context = {
            "admin_user": str(net.get("admin_user") or ""),
            "hostname": hostname,
            "admin_base_url": resolve_admin_base_url(net),
        }

        out_bots: dict[str, dict] = {}
        for bot_id, cfg in bots.items():
            if not isinstance(cfg, dict):
                continue
            multi_user = bool(cfg.get("multiUser", False))
            primary = cfg.get("primary_user") or {}
            primary_external = _external_ids.read_external_ids(primary)
            has_primary = bool(primary_external)
            missing: list[str] = []
            if multi_user and not has_admin:
                # pod-wide; surfaced on every multi-user bot for clarity
                missing.append("admin")
            if multi_user and not has_primary:
                missing.append("primary")
            out_bots[bot_id] = {
                "multi_user": multi_user,
                "role": cfg.get("role"),
                "primary_user": primary,
                "primary_passphrase_override":
                    bot_primary_passphrase_override(net, bot_id),
                "missing": missing,
            }

        return jsonify({
            "pod_context": pod_context,
            "pod_admins": admins,
            "pod_passphrases": {
                # Echoing the live values is intentional — the operator is
                # already authenticated for the admin UI, and the UI needs
                # to show "current value" alongside the Edit affordance.
                # The wire is HTTPS in production; same secrecy bar as
                # every other secret in the admin UI (API keys etc.).
                "admin": admin_passphrase(net),
                "primary": primary_passphrase(net),
            },
            "bots": out_bots,
            "members": list(net.get("members") or []),
        })

    @app.post("/api/admin/identity/claim-primary")
    def identity_claim_primary() -> Any:  # type: ignore[no-redef]
        body = request.get_json(silent=True) or {}
        bot_id = body.get("bot_id")
        channel = body.get("channel")
        external_id = body.get("external_id")
        pod_user = body.get("pod_user")
        name = body.get("name")
        force = bool(body.get("force", False))

        if not isinstance(bot_id, str) or not bot_id:
            return jsonify({"error": "missing or invalid 'bot_id'"}), 400
        if not isinstance(channel, str) or not channel.strip():
            return jsonify({"error": "missing or invalid 'channel'"}), 400
        if not isinstance(external_id, str) or not external_id.strip():
            return jsonify({"error": "missing or invalid 'external_id'"}), 400

        net = load_network(network_path)
        # Capability gate. This route writes `bots.<id>.primary_user` AND runs
        # seed_channel_identity, which auto-approves the id into the bot's
        # allowFrom — the role=primary and the admission branches of the
        # CBR-1.2 class in one call. Placed after the body validation (so a
        # malformed request still answers 400) and before any mutation.
        ok, reason = _check_capability(
            net, bot_id, _IDENTITY_WRITE_CAPABILITY)
        if not ok:
            return jsonify({"error": "forbidden", "detail": reason}), 403
        try:
            primary_block = claim_primary(
                net, bot_id, channel, external_id,
                pod_user=pod_user if isinstance(pod_user, str) and pod_user else None,
                name=name if isinstance(name, str) and name.strip() else None,
                force=force,
            )
        except ClaimError as e:
            # ClaimError covers "bot not a pod member" and
            # "already recorded — pass force=true" — both client-fixable.
            return jsonify({"error": str(e)}), 400

        # Setting an owner should make the bot able to TALK to that owner —
        # not just record the name. Run the full coherence primitive so the
        # Set-owner action ALSO auto-approves the owner's DM (allowFrom) and
        # records the chat_id where the Credentials UI reads it. This is how
        # an already-broken pod self-heals (owner recorded but the bot is
        # mute) without a fresh install, and makes "owner set but no DM
        # approval" drift structurally impossible going forward. The
        # newcomer "Require approval" policy still gates everyone but the
        # recorded owner. claim_primary already mutated `net`; seed reuses it
        # for the owner step (a no-op now) and persists the bot-side files.
        seed_errors: list[str] = []
        try:
            from ..evo.seed_identity import seed_channel_identity
            seed_res = seed_channel_identity(
                net, bot_id,
                channel.strip().lower(), external_id.strip(),
                network_path=network_path,
                display_name=name if isinstance(name, str) and name.strip() else None,
            )
            seed_errors = list(seed_res.errors)
        except Exception as e:  # noqa: BLE001 — never sink the owner write
            seed_errors = [str(e)]

        save_network(net, network_path)
        return jsonify({
            "ok": True,
            "primary_user": primary_block,
            # Surface partial-failure of the DM-approval / chat_id side so the
            # UI can warn ("owner saved, but DM auto-approve failed: …")
            # rather than claim full success. Empty list = clean seed.
            "seed_warnings": seed_errors,
        })

    @app.post("/api/admin/identity/claim-admin")
    def identity_claim_admin() -> Any:  # type: ignore[no-redef]
        body = request.get_json(silent=True) or {}
        channel = body.get("channel")
        external_id = body.get("external_id")
        pod_user = body.get("pod_user")
        name = body.get("name")

        if not isinstance(channel, str) or not channel.strip():
            return jsonify({"error": "missing or invalid 'channel'"}), 400
        if not isinstance(external_id, str) or not external_id.strip():
            return jsonify({"error": "missing or invalid 'external_id'"}), 400

        net = load_network(network_path)
        # Pod-scoped gate. This is byte-for-byte the same write as
        # routes_pairing._promote_to_pod_admin — which is NOT gated on main;
        # #3643 proposes that gate and is still OPEN, so this route does not
        # depend on it — but strictly easier to reach: no pending pairing
        # request, no validate_id() format check, no known_channels()
        # membership. A pod-admin claim resolves to role
        # "admin" — hence ["*"] — on EVERY bot, so an ungated route here is the
        # key that opens every capability gate elsewhere.
        ok, reason = _check_pod_admin(net, "admin claim")
        if not ok:
            return jsonify({"error": "forbidden", "detail": reason}), 403
        try:
            admins = claim_admin(
                net,
                channel=channel, external_id=external_id,
                pod_user=pod_user if isinstance(pod_user, str) and pod_user else None,
                name=name if isinstance(name, str) and name.strip() else None,
            )
        except ClaimError as e:
            return jsonify({"error": str(e)}), 400
        save_network(net, network_path)
        return jsonify({"ok": True, "admins": admins})

    @app.post("/api/admin/identity/revoke-admin")
    def identity_revoke_admin() -> Any:  # type: ignore[no-redef]
        body = request.get_json(silent=True) or {}
        channel = body.get("channel")
        external_id = body.get("external_id")
        drop_pod_user = body.get("drop_pod_user")
        if not isinstance(channel, str) or not channel.strip():
            return jsonify({"error": "missing or invalid 'channel'"}), 400
        if not isinstance(external_id, str) or not external_id.strip():
            return jsonify({"error": "missing or invalid 'external_id'"}), 400
        net = load_network(network_path)
        # Pod-scoped gate — the inverse write, and a lockout / denial-of-
        # service primitive in its own right (strip every pod admin and the
        # operator loses the admin tier pod-wide).
        ok, reason = _check_pod_admin(net, "admin revoke")
        if not ok:
            return jsonify({"error": "forbidden", "detail": reason}), 403
        try:
            removed = revoke_admin(
                net, channel=channel, external_id=external_id,
                drop_pod_user=(
                    drop_pod_user
                    if isinstance(drop_pod_user, str) and drop_pod_user
                    else None
                ),
            )
        except ClaimError as e:
            return jsonify({"error": str(e)}), 400
        if removed:
            save_network(net, network_path)
        return jsonify({"ok": True, "removed": removed})

    @app.post("/api/admin/identity/set-pod-passphrase")
    def identity_set_pod_passphrase() -> Any:  # type: ignore[no-redef]
        body = request.get_json(silent=True) or {}
        kind = body.get("kind")
        passphrase = body.get("passphrase")
        if kind not in ("admin", "primary"):
            return jsonify({"error": "kind must be 'admin' or 'primary'"}), 400
        # passphrase may be empty/null to clear; the identity helper
        # handles that branch.
        if passphrase is not None and not isinstance(passphrase, str):
            return jsonify({"error": "passphrase must be a string"}), 400
        net = load_network(network_path)
        if kind == "admin":
            set_pod_admin_passphrase(net, passphrase)
        else:
            set_pod_primary_passphrase(net, passphrase)
        save_network(net, network_path)
        return jsonify({"ok": True})

    @app.post("/api/admin/identity/set-bot-passphrase")
    def identity_set_bot_passphrase() -> Any:  # type: ignore[no-redef]
        body = request.get_json(silent=True) or {}
        bot_id = body.get("bot_id")
        passphrase = body.get("passphrase")
        if not isinstance(bot_id, str) or not bot_id:
            return jsonify({"error": "missing or invalid 'bot_id'"}), 400
        if passphrase is not None and not isinstance(passphrase, str):
            return jsonify({"error": "passphrase must be a string"}), 400
        net = load_network(network_path)
        try:
            set_bot_primary_passphrase(net, bot_id, passphrase)
        except ClaimError as e:
            return jsonify({"error": str(e)}), 400
        save_network(net, network_path)
        return jsonify({"ok": True})

    @app.post("/api/admin/identity/resolve-name")
    def identity_resolve_name() -> Any:  # type: ignore[no-redef]
        """Look up a human-readable name for a messaging identity.

        Body:
          channel             "telegram" | "slack" (others currently
                              return ok:false with a clear reason)
          external_id         the platform-specific user id
          refresh             optional bool — when true, bypass the
                              cache and force a fresh API call

        Returns ``{ok: True, resolved: {username, name, cached_at}}``
        on success, or ``{ok: False, reason: "..."}`` when:
          - channel isn't supported
          - no bot in the pod has that channel configured
          - the platform API call failed (user not found / token
            invalid / network error)

        The caller (the Identity subtab) renders ok:false as the raw
        ID with a tooltip explaining why no name is available.
        """
        body = request.get_json(silent=True) or {}
        channel = body.get("channel")
        external_id = body.get("external_id")
        refresh = bool(body.get("refresh", False))
        if not isinstance(channel, str) or not channel.strip():
            return jsonify({"ok": False,
                            "reason": "channel is required"}), 400
        if not isinstance(external_id, str) or not external_id.strip():
            return jsonify({"ok": False,
                            "reason": "external_id is required"}), 400
        channel = channel.strip().lower()
        external_id = external_id.strip()

        if channel not in name_resolver.SUPPORTED_CHANNELS:
            return jsonify({
                "ok": False,
                "reason": (
                    f"{channel!r} lookup not supported yet — supported "
                    f"channels are: "
                    f"{sorted(name_resolver.SUPPORTED_CHANNELS)}"
                ),
            })

        net = load_network(network_path)
        resolved = name_resolver.resolve(
            net,
            channel=channel,
            external_id=external_id,
            use_cache=not refresh,
        )
        if resolved is None:
            return jsonify({
                "ok": False,
                "reason": (
                    "Could not resolve the user. Either no bot has the "
                    "channel configured, the platform API rejected the "
                    "lookup, or the user hasn't interacted with the bot "
                    "yet."
                ),
            })

        # name_resolver.resolve writes to network on a fresh fetch;
        # persist so the cache survives a restart.
        save_network(net, network_path)
        return jsonify({"ok": True, "resolved": resolved})

    @app.post("/api/admin/identity/clear-primary")
    def identity_clear_primary() -> Any:  # type: ignore[no-redef]
        """Remove the recorded primary_user block on a bot.

        Body: ``{bot_id: <str>}``
        Returns: ``{ok: True, cleared: <bool>}`` — ``cleared=False``
        when the bot had no primary recorded (idempotent no-op).

        Use this when transferring a bot's owner — the operator can
        then re-set the primary via the normal claim form. Distinct
        from ``revoke-admin`` (which removes channel IDs from the
        pod-wide admin list).
        """
        body = request.get_json(silent=True) or {}
        bot_id = body.get("bot_id")
        if not isinstance(bot_id, str) or not bot_id:
            return jsonify({"error": "missing or invalid 'bot_id'"}), 400
        net = load_network(network_path)
        # Capability gate — clearing primary_user is an ownership mutation
        # (it un-owns the bot), so it takes the same floor as claim-primary.
        ok, reason = _check_capability(
            net, bot_id, _IDENTITY_WRITE_CAPABILITY)
        if not ok:
            return jsonify({"error": "forbidden", "detail": reason}), 403
        try:
            cleared = clear_primary(net, bot_id)
        except ClaimError as e:
            return jsonify({"error": str(e)}), 400
        if cleared:
            save_network(net, network_path)
        return jsonify({"ok": True, "cleared": cleared})

    @app.post("/api/admin/identity/discover-primary")
    def identity_discover_primary() -> Any:  # type: ignore[no-redef]
        """Auto-discover candidate primary-user identities from turn history.

        Body:
          bot_id          (required) the bot to scan
          lookback_days   (optional, default 30) how far back to scan
          top_k           (optional, default 5) max candidates returned

        Returns ``{ok: True, candidates: [...]}`` with each candidate
        carrying channel + external_id + turn_count + first/last_seen,
        plus resolved username/display_name where the platform supports
        it (Telegram + Slack today).

        The candidate list is sorted by turn count desc, then last_seen
        desc as tiebreaker — most active + most recent ranks highest.
        Empty list when the bot has no human turn history (newly-
        deployed; caller should fall through to manual entry).

        Used by the Identity subtab's "Discover from history" button
        and (planned) the setup wizard's owner-capture step.
        """
        body = request.get_json(silent=True) or {}
        bot_id = body.get("bot_id")
        if not isinstance(bot_id, str) or not bot_id:
            return jsonify({"error": "missing or invalid 'bot_id'"}), 400
        lookback_days = body.get("lookback_days", 30)
        if not isinstance(lookback_days, int) or lookback_days < 1:
            lookback_days = 30
        top_k = body.get("top_k", 5)
        if not isinstance(top_k, int) or top_k < 1:
            top_k = 5

        net = load_network(network_path)
        bots = net.get("bots") or {}
        bot_cfg = bots.get(bot_id)
        if not isinstance(bot_cfg, dict):
            return jsonify({
                "error": f"unknown bot_id {bot_id!r}",
            }), 400
        bot_user = bot_cfg.get("user") or bot_id

        candidates = identity_discovery.discover_candidates(
            bot_id, bot_user=bot_user,
            lookback_days=lookback_days, top_k=top_k,
        )
        # Run each candidate through the name resolver — silent on
        # failure (the candidate just renders without a name). This
        # may write to the resolved_names cache; persist below.
        #
        # bot_id threads through so Slack D-channel candidates resolve
        # through THIS bot's token (workspace-scoped) and the
        # subsequent users.info call uses the same token. Without
        # bot_id the resolver would fall back to primary's token,
        # which fails for cross-workspace bots.
        resolved = identity_discovery.resolve_with_names(
            net, candidates, bot_id=bot_id)
        save_network(net, network_path)
        return jsonify({"ok": True, "candidates": resolved})
