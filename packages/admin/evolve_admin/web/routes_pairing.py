"""Admin per-bot messaging-ID pairing routes.

Backs the admin-UI pairing wizard (modal launched from the Overview
tile chip, install-wizard Done screen, or a deep link). The wizard's
primary input is the *pairing code* the bot sent the operator after
they DMd it — codes are unique per pending request, so they
disambiguate identity even when multiple people pair against the
same bot at once.

Endpoints (under ``/api/admin/bots/<bot_id>/pairing``):

  GET  /lookup?code=<code>         find a pending request by code
                                   across all configured channels for
                                   this bot. Returns identity (id,
                                   meta) so the modal can confirm.

  POST /commit                     finish pairing. Body:
                                     {channel, id, role, name?}
                                   Routes the write by role:
                                     pod_admin → pod.admins.external_ids
                                     primary   → bots.<bot>.primary_user
                                     other     → just allowFrom (no
                                                 pod-wide promotion)
                                   Then approves the ID into the bot's
                                   channel allowFrom file (reusing the
                                   existing _approve helper).

  GET  /state?channel=<ch>         lightweight status — is anyone
                                   currently paired? Used by the modal
                                   to render "Paired as X" once a
                                   commit lands.

Spec: pairing flow design discussion 2026-06-01. OC pairing meta
shape verified against /opt/homebrew/lib/node_modules/openclaw/dist/
bot-iSDqdz0Y.js:1380 (telegram), :1380-section equiv for other
channels.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from flask import Flask, jsonify, request

from ..config import load_network, save_network
from ..pairing.config import (
    get_channel_config,
    known_channels,
    all_ui_dicts,
)
from . import routes_bot_users as bot_users


log = logging.getLogger(__name__)


def register_routes(app: Flask, network_path: Path) -> None:
    """Register pairing endpoints on ``app``.

    Mirrors the shape of ``routes_bot_users.register_routes`` — same
    network_path injection, same Flask decorator style. Body validation
    is manual (no pydantic) to match the rest of the admin server.
    """

    @app.get("/api/admin/pairing/config")
    def pairing_config_index() -> Any:  # type: ignore[no-redef]
        """Return the per-channel UI config table.

        The admin-UI bundle fetches this once at boot and uses it to
        render the modal's per-channel copy (labels, hints, validator
        regex, deeplink-template availability). Keeping the JS side a
        consumer of the Python truth source avoids drift across the
        modal, the install wizard, and the tile chip.
        """
        return jsonify({"channels": all_ui_dicts()})

    @app.get("/api/admin/bots/<bot_id>/pairing/lookup")
    def pairing_lookup(bot_id: str) -> Any:  # type: ignore[no-redef]
        code = (request.args.get("code") or "").strip()
        if not code:
            return jsonify({"error": "missing 'code'"}), 400
        net = load_network(network_path)
        if not bot_users._bot_exists(net, bot_id):
            return jsonify({"error": f"unknown bot: {bot_id}"}), 404
        hit = _find_pending_by_code(net, bot_id, code)
        if hit is None:
            # found=false is a normal response, not an error — the
            # operator might be early (bot hasn't written the request
            # yet) or pasted a wrong code. The modal asks them to
            # try again.
            return jsonify({"found": False})
        ch, req = hit
        meta = req.get("meta") or {}
        display_name = bot_users._meta_to_display_name(ch, meta)
        return jsonify({
            "found": True,
            "channel": ch,
            "id": req.get("id"),
            "code": req.get("code"),
            "display_name": display_name,
            "meta": meta,
        })

    @app.post("/api/admin/bots/<bot_id>/pairing/commit")
    def pairing_commit(bot_id: str) -> Any:  # type: ignore[no-redef]
        body = request.get_json(silent=True) or {}
        channel = (body.get("channel") or "").strip().lower()
        ext_id = (body.get("id") or "").strip()
        role = (body.get("role") or "").strip()
        name = (body.get("name") or "").strip() or None
        if channel not in known_channels():
            return jsonify({"error": "missing or invalid 'channel'"}), 400
        cfg = get_channel_config(channel)
        if not cfg or not cfg.validate_id(ext_id):
            return jsonify({
                "error": "missing or invalid 'id'",
                "detail": cfg.id_format_hint if cfg else None,
            }), 400
        if role not in ("pod_admin", "primary", "other"):
            return jsonify({"error": "invalid 'role'"}), 400
        net = load_network(network_path)
        if not bot_users._bot_exists(net, bot_id):
            return jsonify({"error": f"unknown bot: {bot_id}"}), 404
        try:
            _commit_pairing(net, network_path, bot_id, channel,
                            ext_id, role, name)
        except bot_users._PairingError as e:
            return jsonify({"error": str(e)}), 400
        except (PermissionError, OSError) as e:
            log.exception("pairing commit %s/%s/%s failed", bot_id, channel, ext_id)
            return jsonify({
                "error": "commit_failed",
                "detail": str(e),
            }), 500
        # Re-read the per-channel state so the modal can render
        # "paired as X" without a separate round trip.
        by_channel = bot_users._read_per_channel(net, bot_id)
        return jsonify({
            "ok": True,
            "bot_id": bot_id,
            "channel": channel,
            "id": ext_id,
            "role": role,
            "by_channel": by_channel,
        })

    @app.get("/api/admin/bots/<bot_id>/pairing/state")
    def pairing_state(bot_id: str) -> Any:  # type: ignore[no-redef]
        """Lightweight status for one bot+channel.

        Returns ``{paired: bool, approved_count, pending_count}``.
        The modal calls this after commit (and on open) to confirm
        the post-state. The tile chip uses the same underlying read
        via ``compute_pairing_chip_for_bot`` (in tile_metrics).
        """
        channel = (request.args.get("channel") or "").strip().lower()
        net = load_network(network_path)
        if not bot_users._bot_exists(net, bot_id):
            return jsonify({"error": f"unknown bot: {bot_id}"}), 404
        out: dict[str, Any] = {"bot_id": bot_id}
        channels_to_check = (
            [channel] if channel in known_channels() else known_channels()
        )
        per: dict[str, dict] = {}
        for ch in channels_to_check:
            ap = bot_users._allowfrom_path(net, bot_id, ch)
            allow = bot_users._read_json_or_default(
                ap, {"version": 1, "allowFrom": []})
            pp = bot_users._pairing_path(net, bot_id, ch)
            pairing = bot_users._read_json_or_default(
                pp, {"version": 1, "requests": []})
            approved = [x for x in (allow.get("allowFrom") or []) if x]
            pending = [r for r in (pairing.get("requests") or [])
                       if r.get("id")]
            per[ch] = {
                "paired": len(approved) > 0,
                "approved_count": len(approved),
                "pending_count": len(pending),
            }
        out["channels"] = per
        return jsonify(out)


# ── Lookup helpers ──────────────────────────────────────────────────────

def _find_pending_by_code(
    network: dict, bot_id: str, code: str,
) -> Optional[tuple[str, dict]]:
    """Scan all configured channels for a pending request with this code.

    Returns ``(channel, request_dict)`` on first match, else None.
    OC generates codes per request (random short tokens, see
    pairing-token-CUzOg9u-.js), so collisions across channels are
    vanishingly unlikely — but if they ever happened, we return the
    first channel hit in known_channels() order, which is stable.
    """
    needle = code.strip()
    if not needle:
        return None
    for ch in known_channels():
        pp = bot_users._pairing_path(network, bot_id, ch)
        pairing = bot_users._read_json_or_none(pp)
        if pairing is None:
            continue
        for req in (pairing.get("requests") or []):
            if not isinstance(req, dict):
                continue
            req_code = (req.get("code") or "").strip()
            if req_code and req_code == needle:
                return ch, req
    return None


# ── Commit helpers ──────────────────────────────────────────────────────

def _commit_pairing(
    network: dict,
    network_path: Path,
    bot_id: str,
    channel: str,
    ext_id: str,
    role: str,
    name: Optional[str],
) -> None:
    """Route the pairing write by role, then approve into allowFrom.

    Three roles, each with a different network.json write footprint
    before the shared approve step:

      pod_admin
        Adds ``ext_id`` to ``pod.admins.external_ids.<channel>`` and
        (when ``name`` is provided) writes a ``pod.admins.resolved_names
        [<channel>:<ext_id>]`` cache entry so the Users page renders
        the operator's name without a channel-API round trip. This
        promotion is pod-wide: the ID is auto-approved for every
        future bot, not just this one. Use sparingly.

      primary
        Sets ``bots.<bot>.primary_user.external_ids.<channel> = ext_id``
        and (when ``name`` is provided) ``primary_user.name``. Per-bot
        only — primary is a single-user concept; the wizard validates
        there isn't already a different primary set. (We *replace*
        rather than refuse, on the assumption that the operator
        deliberately re-paired; the previous primary stays in
        allowFrom and can be revoked from the Users page.)

      other
        No network.json change. Just approves the ID into the bot's
        ``<channel>-default-allowFrom.json`` — same as if the
        operator clicked Approve on the Users page.

    After the role-specific write, calls ``routes_bot_users._approve``
    to land the ID in allowFrom. ``_approve`` also captures the
    pairing-time meta into the identity_cache for name rendering on
    future Users-page reads — we don't re-do that here.
    """
    if role == "pod_admin":
        _promote_to_pod_admin(network, channel, ext_id, name)
        save_network(network, network_path)
    elif role == "primary":
        _set_primary_user(network, bot_id, channel, ext_id, name)
        save_network(network, network_path)
    elif role == "other":
        # No network.json change — the ID is just approved into the
        # per-bot allowFrom.json below.
        pass
    else:
        raise bot_users._PairingError(f"unknown role: {role}")

    # Shared approve step. ``_approve`` handles:
    #   - capturing pairing meta into identity_cache (for name display)
    #   - dropping pending entry from <ch>-pairing.json
    #   - adding ID to <ch>-default-allowFrom.json
    #   - bot-user-owned file writes via /tmp + sudo /bin/cp + chown
    # Pass code=None — _approve uses it only for the "reject specific
    # code" path, which isn't relevant here.
    bot_users._approve(network, bot_id, channel, ext_id, code=None)


def _promote_to_pod_admin(
    network: dict, channel: str, ext_id: str, name: Optional[str],
) -> None:
    """Add ext_id to pod.admins.external_ids.<channel> + resolved_names.

    Idempotent: if ext_id is already a pod admin for this channel,
    only the resolved_names update may happen (when ``name`` is fresh).
    """
    pod = network.setdefault("pod", {})
    admins = pod.setdefault("admins", {})
    ext = admins.setdefault("external_ids", {})
    current = ext.get(channel) or []
    if not isinstance(current, list):
        current = []
    if ext_id not in current:
        current.append(ext_id)
    ext[channel] = current
    if name:
        resolved = admins.setdefault("resolved_names", {})
        key = f"{channel}:{ext_id}"
        # Preserve any existing email/username fields if we had them
        prev = resolved.get(key) or {}
        prev["name"] = name
        prev["source"] = "pairing_wizard"
        resolved[key] = prev


def _set_primary_user(
    network: dict, bot_id: str, channel: str, ext_id: str,
    name: Optional[str],
) -> None:
    """Set bots.<bot>.primary_user.external_ids.<channel> = ext_id.

    Also writes ``primary_user.name`` when provided. Replaces any
    existing primary for that channel — the operator is explicitly
    re-pairing, and the prior primary (if any) stays in allowFrom
    until manually revoked from the Users page.
    """
    bots = network.setdefault("bots", {})
    bot = bots.setdefault(bot_id, {})
    primary = bot.setdefault("primary_user", {})
    ext = primary.setdefault("external_ids", {})
    ext[channel] = ext_id
    if name:
        primary["name"] = name
