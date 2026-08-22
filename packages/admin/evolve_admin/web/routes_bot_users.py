"""Admin per-bot paired-users routes.

Surfaces OpenClaw's per-bot pairing state in the admin UI's Users page.
Each bot has, per provider, two on-disk files under
``<bot_home>/.openclaw/credentials/``:

  ``<provider>-pairing.json``::

      {"version": 1,
       "requests": [{"id", "code", "createdAt", "lastSeenAt", "meta": {...}}, ...]}

  ``<provider>-default-allowFrom.json``::

      {"version": 1, "allowFrom": [<channel-native id>, ...]}

The admin server (``evolve`` user) has ACL read on the parent dir, so
reads are plain ``Path.read_text``. Writes go through ``/tmp`` +
``sudo /bin/cp`` + ``sudo /bin/chmod 600`` per the CLAUDE.md
bot-owned-file pattern. The sudoers grant is added by
``setup_wizard._render_evolve_sudoers`` in the same PR as this module.

Endpoints (all under ``/api/admin/bots/<bot_id>/users``):

  GET  /                  → ``{bot_id, by_channel: {<ch>: {supported, approved[], group_access[], pending[], ...}}}``
  POST /approve           → body ``{channel, id, code?}``  → re-renders by_channel
  POST /revoke            → body ``{channel, id}``         → re-renders by_channel
  POST /reject            → body ``{channel, id, code?}``  → re-renders by_channel
  POST /group-allowlist/approve → body ``{channel, id}``   → re-renders by_channel
  POST /group-allowlist/revoke  → body ``{channel, id}``   → re-renders by_channel

The two ``group-allowlist`` routes (R1a PR2) manage the config-level group
allowlist (``openclaw.json::channels.<ch>`` under ``groupPolicy: allowlist``)
— a SEPARATE OpenClaw gate from the credentials DM pairing store the
approve/revoke routes manage. They write ONLY openclaw.json (via the
schema-validating ``deploy.safe_write_bot_config`` + a gateway kick), never the
DM store, so the two lists stay strictly separate.

GET also runs an inline auto-approval sweep before rendering: any
pending request whose ``id`` matches a pod-admin claim for that channel
(``network.json::pod.admins.external_ids[<ch>]``) is approved in place,
so the operator sees their own ``/start`` already-approved when they
land on the page.

Spec: docs/spec-per-bot-users-management-2026-05-29.md
"""

from __future__ import annotations

import copy
import enum
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request

from platform_profile import get_profile as _get_profile

from ..config import bot_home, get_bot_user, load_network, save_network
from . import admin_auth
from .. import capabilities as cap
from .. import channel_registry as _channel_registry
from .. import external_ids as _external_ids
from .. import roster_identity as ri
from .. import roster_overlay as ro
from .. import roster_resolver as rr
from .. import user_activity as ua
from ..user_directory import resolver as udr
from ..user_directory import storage as uds


log = logging.getLogger(__name__)


# Providers we surface in the Users UI. Matches the OpenClaw
# on-disk filename prefixes seen on the deployed pod (2026-05-29):
# ``telegram-pairing.json``, ``slack-pairing.json``,
# ``whatsapp-pairing.json``. Discord is included on the strength of
# a Discord-attached bot's integration; missing files are treated as
# ``supported=False`` and the channel is hidden in the UI.
#
# ``signal`` is intentionally absent — Evolve has no Signal integration
# and rendering it as an empty-state placeholder was confusing operators
# (2026-05-29 mini feedback). Add back when a real Signal integration
# lands.
# Projection of the channel registry's ``pairing`` column. Signal's absence is
# now a registry fact (its row carries ``pairing=None`` with the 2026-05-29
# rationale) rather than a comment on a hand-maintained tuple — give Signal a
# ChannelConfig in the registry and it appears here automatically.
KNOWN_PROVIDERS: tuple[str, ...] = _channel_registry.pairing_channel_ids()


class _PairingError(Exception):
    """Surfaced as a 400 to the admin UI."""


class _GroupWriteError(Exception):
    """Surfaced as a 500 to the admin UI — the openclaw.json write itself
    failed (schema-validation reject, sudo grant missing, disk error). Kept
    distinct from ``_PairingError`` (a 400: the *request* is invalid — e.g. the
    channel isn't allowlist-gated) so the route maps each to the right status."""


def register_routes(app: Flask, network_path: Path) -> None:
    """Register per-bot users endpoints on ``app``."""

    @app.get("/api/admin/bots/<bot_id>/users")
    def bot_users_list(bot_id: str) -> Any:  # type: ignore[no-redef]
        net = load_network(network_path)
        if not _bot_exists(net, bot_id):
            return jsonify({"error": f"unknown bot: {bot_id}"}), 404
        # Best-effort auto-approval of known pod admins so the page
        # reflects the post-sweep state. Failures don't block the read.
        try:
            _auto_approve_inline(net, bot_id)
        except Exception as e:  # noqa: BLE001
            log.warning("auto-approve inline sweep failed for %s: %s", bot_id, e)
        by_channel = _read_per_channel(net, bot_id)
        # Phase 2 name enrichment: for approved IDs that have no display
        # name from any local source (resolved_names cache, admins.names,
        # primary, identity_cache), call the channel API directly via
        # name_resolver.resolve. Concurrent with a short budget so we
        # don't slow down the page load too much; stragglers complete
        # the next GET cycle. Persists any newly-resolved names into
        # ``pod.admins.resolved_names`` so they show up everywhere.
        try:
            if _enrich_unknown_names(
                    net, by_channel, max_seconds=5.0, bot_id=bot_id):
                save_network(net, network_path)
        except Exception as e:  # noqa: BLE001
            log.warning("name enrichment failed for %s: %s", bot_id, e)
        # Spec 2026-06-07 §9: surface the blocked-identity list as a
        # top-level array so the UI can render a "Blocked users"
        # section without scanning approved[]. Each entry carries the
        # platform + stable_id + audit metadata.
        overlay = ro.load_overlay(_shared_dir_from_net(net), bot_id)
        blocked = _blocked_list(overlay)
        return jsonify({
            "bot_id": bot_id,
            "by_channel": by_channel,
            "blocked": blocked,
        })

    @app.get("/api/admin/bots/<bot_id>/users/tier-prefs")
    def bot_users_tier_prefs(bot_id: str) -> Any:  # type: ignore[no-redef]
        """Per-user standing tier defaults for one bot (read-only).

        G5 of the spec-user-tier-control 2026-08-03 addendum: surface
        what ``evo tier-default`` wrote to
        ``{sharedDir}/{botId}/user-tier-prefs.json`` so the pod admin
        can SEE per-user standing defaults on the Users page. Missing or
        malformed file → empty list (``user_tier_prefs._load`` never
        raises), so pods with no prefs render the empty state, not an
        error. Read surface only — writes stay with the evo
        ``tier-default`` handler (and G4's future bot-invocable tool).
        """
        net = load_network(network_path)
        if not _bot_exists(net, bot_id):
            return jsonify({"error": f"unknown bot: {bot_id}"}), 404
        return jsonify({
            "bot_id": bot_id,
            "users": _tier_pref_rows(_shared_dir_from_net(net), bot_id),
        })

    @app.post("/api/admin/bots/<bot_id>/users/approve")
    def bot_users_approve(bot_id: str) -> Any:  # type: ignore[no-redef]
        return _handle_action(network_path, bot_id, _approve, require_id=True)

    @app.post("/api/admin/bots/<bot_id>/users/revoke")
    def bot_users_revoke(bot_id: str) -> Any:  # type: ignore[no-redef]
        return _handle_action(network_path, bot_id, _revoke, require_id=True)

    @app.post("/api/admin/bots/<bot_id>/users/reject")
    def bot_users_reject(bot_id: str) -> Any:  # type: ignore[no-redef]
        return _handle_action(network_path, bot_id, _reject, require_id=True)

    # ── Group/channel allowlist management (R1a PR2, 2026-06-18) ─────────
    #
    # The config-level group allowlist (openclaw.json::channels.<ch> under
    # ``groupPolicy: allowlist``) is a SEPARATE OpenClaw gate from the
    # credentials DM pairing store the approve/revoke routes above manage.
    # PR1 (#2999) surfaced it read-only; these two endpoints make it
    # manageable. They write ONLY openclaw.json (never the DM store), so the
    # two lists stay strictly separate by construction — a group approve does
    # not grant DM access and vice-versa. Body: ``{channel, id}``. Distinct
    # path segment (``group-allowlist``) so it can never be mistaken for the
    # ``/users/<channel>/<ext_id>`` overlay routes.

    @app.post("/api/admin/bots/<bot_id>/users/group-allowlist/approve")
    def bot_users_group_approve(bot_id: str) -> Any:  # type: ignore[no-redef]
        return _handle_group_action(network_path, bot_id, add=True)

    @app.post("/api/admin/bots/<bot_id>/users/group-allowlist/revoke")
    def bot_users_group_revoke(bot_id: str) -> Any:  # type: ignore[no-redef]
        return _handle_group_action(network_path, bot_id, add=False)

    # ── Overlay mutations (spec 2026-06-07) ─────────────────────────────

    @app.patch("/api/admin/bots/<bot_id>/users/<channel>/<ext_id>")
    def bot_users_patch(bot_id: str, channel: str,
                        ext_id: str) -> Any:  # type: ignore[no-redef]
        """Update role / engagement_surfaces / notes for an identity.

        Body: ``{"role"?: str, "engagement_surfaces"?: [str], "notes"?: str}``

        ``role="blocked"`` is rejected with 400 — use the dedicated
        ``/block`` endpoint, which also revokes from the OC allowlist
        and clears any pending pairing requests.
        """
        return _handle_overlay_patch(
            network_path, bot_id, channel, ext_id)

    @app.post("/api/admin/bots/<bot_id>/users/<channel>/<ext_id>/block")
    def bot_users_block(bot_id: str, channel: str,
                        ext_id: str) -> Any:  # type: ignore[no-redef]
        """Sticky block: add to overlay block index AND revoke from OC
        allowFrom AND clear pending pairing requests.

        Three-step action because the overlay block alone wouldn't
        prevent re-pairing (the existing pairing flow doesn't consult
        the overlay yet — Layer 1 wiring lands in a follow-up). For
        Phase A, the operator-visible state matches expectation: the
        user disappears from the approved list, can't re-pair without
        being explicitly unblocked, and shows up in the blocked list.
        """
        return _handle_overlay_block(network_path, bot_id, channel, ext_id)

    @app.post("/api/admin/bots/<bot_id>/users/<channel>/<ext_id>/unblock")
    def bot_users_unblock(bot_id: str, channel: str,
                          ext_id: str) -> Any:  # type: ignore[no-redef]
        """Remove from overlay block index. Does NOT re-admit — that
        requires a fresh pairing or explicit add."""
        return _handle_overlay_unblock(
            network_path, bot_id, channel, ext_id)

    @app.post("/api/admin/bots/<bot_id>/users/<channel>/<ext_id>/ignore")
    def bot_users_ignore(bot_id: str, channel: str,
                         ext_id: str) -> Any:  # type: ignore[no-redef]
        """Sticky-dismiss an "Active · not admitted" identity.

        Adds the identity to the overlay ignore index so it stops
        appearing in the seen-recently triage list. Unlike ``/block``
        this does NOT revoke admission or touch the OC allowlist — it's
        a UI-only "I've seen this, hide it" action."""
        return _handle_overlay_ignore(network_path, bot_id, channel, ext_id)

    @app.put("/api/admin/bots/<bot_id>/channels/<channel>/newcomer_mode")
    def channel_newcomer_mode_put(
            bot_id: str, channel: str) -> Any:  # type: ignore[no-redef]
        """Per-channel newcomer mode + optional default engagement
        surfaces. Body: ``{"mode": str, "default_engagement_surfaces"?:
        [str]}``."""
        return _handle_newcomer_mode_put(
            network_path, bot_id, channel)


# ── Action dispatcher ────────────────────────────────────────────────────


def _handle_action(network_path: Path, bot_id: str, fn, *, require_id: bool):
    """Shared handler for the DM pairing mutations (approve / revoke / reject).

    Gated on ``bot.roster.mutate`` — admitting or revoking a DM user IS a
    roster mutation, the same class as the overlay patch/block/unblock
    handlers below (WO-H1-2 sibling, CBR-1.2: these three routes were the
    only mutation handlers in the file with no ``_check_capability`` call,
    so the trusted evo peer uid — and, on auth-disabled pods, any local
    bot-gateway uid on the admin socket — could admit DM users header-free).
    Header-absent / malformed semantics are inherited from
    ``_check_capability``: absent is trusted only for the authenticated
    admin UI (the SPA Users page, the sole intended caller of these routes).
    """
    body = request.get_json(silent=True) or {}
    channel = (body.get("channel") or "").strip()
    ext_id = (body.get("id") or "").strip()
    code = (body.get("code") or "").strip() or None
    if not channel or channel not in KNOWN_PROVIDERS:
        return jsonify({"error": "missing or invalid 'channel'"}), 400
    if require_id and not ext_id:
        return jsonify({"error": "missing 'id'"}), 400
    net = load_network(network_path)
    if not _bot_exists(net, bot_id):
        return jsonify({"error": f"unknown bot: {bot_id}"}), 404
    ok, reason = _check_capability(net, bot_id, "bot.roster.mutate")
    if not ok:
        return jsonify({"error": "forbidden", "detail": reason}), 403
    try:
        fn(net, bot_id, channel, ext_id, code)
    except _PairingError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "by_channel": _read_per_channel(net, bot_id)})


def _handle_group_action(network_path: Path, bot_id: str, *, add: bool):
    """Approve (``add=True``) / revoke (``add=False``) a group-allowlist id.

    Mutates ONLY ``openclaw.json::channels.<ch>`` group allowlist — never the
    DM pairing store — so the two lists stay strictly separate (the R1a
    operator invariant). Gated on the ``bot.channel.config`` capability (the
    same gate as the per-channel newcomer-mode write, the other channel-config
    mutation); the gate is a no-op for the UI-trusted path (no
    X-Requester-Identity header) and a real check for gateway-attested callers.

    Returns the re-rendered ``by_channel`` so the page reflects the change
    through the SAME canonical resolver (``rr.read_group_allowlists`` →
    ``effective_group_allowlist``) the GET uses — no second source of truth.
    A best-effort gateway-restart failure (the change is durable on disk but
    not yet live) is surfaced as ``gateway_restart_warning`` rather than rolled
    back; a *write* failure is a hard 500.
    """
    body = request.get_json(silent=True) or {}
    channel = (body.get("channel") or "").strip()
    ext_id = (body.get("id") or "").strip()
    if not channel or channel not in KNOWN_PROVIDERS:
        return jsonify({"error": "missing or invalid 'channel'"}), 400
    if not ext_id:
        return jsonify({"error": "missing 'id'"}), 400
    net = load_network(network_path)
    if not _bot_exists(net, bot_id):
        return jsonify({"error": f"unknown bot: {bot_id}"}), 404
    ok, reason = _check_capability(net, bot_id, "bot.channel.config")
    if not ok:
        return jsonify({"error": "forbidden", "detail": reason}), 403
    try:
        warning = _group_allowlist_write(net, bot_id, channel, ext_id, add=add)
    except _PairingError as e:
        return jsonify({"error": str(e)}), 400
    except _GroupWriteError as e:
        return jsonify({"error": str(e)}), 500
    resp: dict[str, Any] = {
        "ok": True, "by_channel": _read_per_channel(net, bot_id)}
    if warning:
        resp["gateway_restart_warning"] = warning
    return jsonify(resp)


# ── Read side ────────────────────────────────────────────────────────────


def _bot_exists(network: dict, bot_id: str) -> bool:
    return bot_id in (network.get("bots") or {})


def _credentials_dir(network: dict, bot_id: str) -> Path:
    return bot_home(bot_id, network) / ".openclaw" / "credentials"


def _pairing_path(network: dict, bot_id: str, channel: str) -> Path:
    return _credentials_dir(network, bot_id) / f"{channel}-pairing.json"


def _allowfrom_path(network: dict, bot_id: str, channel: str) -> Path:
    return _credentials_dir(network, bot_id) / f"{channel}-default-allowFrom.json"


def _openclaw_json_path(network: dict, bot_id: str) -> Path:
    """The bot's main ``openclaw.json`` — source of the config-level
    ``channels.<ch>`` group allowlist (distinct from the credentials DM
    pairing store). Read via ``_read_json_or_none`` (direct ACL read +
    ``sudo /bin/cat`` fallback), same as the credentials files."""
    return bot_home(bot_id, network) / ".openclaw" / "openclaw.json"


def _read_json_or_none(path: Path) -> "dict | None":
    """Read JSON. Returns the parsed dict on success, ``{}`` on parse
    error, or ``None`` when the file genuinely doesn't exist / isn't
    reachable even via the sudo fallback.

    Why not ``Path.exists()`` first? On macOS, ``exists()`` returns
    False when the calling process can't *traverse* into the parent
    directory — not just when the file is absent. The evolve user
    has an ACL on ``.openclaw/`` but not always on its subdirectories
    (``credentials/`` is created at OC runtime, mode 700), so
    ``exists()`` silently lies and we'd return empty data instead of
    using the ``sudo /bin/cat`` grant. Try the direct read first,
    then the sudo fallback, and only return ``None`` if both fail.
    """
    text: "str | None" = None
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except (PermissionError, OSError):
        text = None
    if text is None:
        try:
            r = subprocess.run(
                ["sudo", "-n", "/bin/cat", str(path)],
                check=True, capture_output=True, text=True, timeout=5,
            )
            text = r.stdout
        except subprocess.CalledProcessError:
            return None
        except (subprocess.TimeoutExpired, OSError):
            return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}


def _read_json_or_default(path: Path, default: dict) -> dict:
    """``_read_json_or_none`` with a fallback default for callers that
    don't need to distinguish 'present but empty' from 'absent'."""
    got = _read_json_or_none(path)
    return got if got is not None else dict(default)


def _read_per_channel(network: dict, bot_id: str) -> dict[str, dict]:
    """Build the ``{channel: {supported, approved[], group_access[],
    pending[], seen_recently[], newcomer_mode,
    default_engagement_surfaces}}`` response.

    Per spec 2026-06-07 §9 extends the response additively with overlay
    fields: each approved entry gets ``role`` and ``engagement_surfaces``
    (resolved against network.json claims + overlay); each channel gets
    ``newcomer_mode`` and ``default_engagement_surfaces``. Existing
    fields (id, display_name, labels, source, email; supported, pending)
    are unchanged. R1a (2026-06-17) adds ``group_access[]`` — the
    config-level group/channel allowlist, a separate gate from the DM
    pairing store the ``approved`` list reads (see ``rr.read_group_allowlists``).
    """
    out: dict[str, dict] = {}
    shared_dir = _shared_dir_from_net(network)
    overlay = ro.load_overlay(shared_dir, bot_id)
    # Directory store (spec-user-directory-2026-06-22 §4) — read ONCE here and
    # passed into resolve_person below so the page does one directory read, not
    # one per identity. Soft-loads to the empty shape when absent/unreadable.
    directory = uds.load_directory(shared_dir, bot_id)
    # Phase D.2 — per-user activity (last_seen + turns_7d) from the
    # bot's turn rollups. Joined into each approved entry by
    # ``<channel>:<id>`` key. Aggregator soft-fails to empty dict
    # when turns dir is missing or unreadable; the join becomes a
    # no-op and entries simply omit last_seen.
    try:
        activity = ua.aggregate(shared_dir, bot_id)
        # G.6 — rewrite Slack D-channel activity keys to U-user keys
        # so the join with the approved list (keyed by U-id) hits.
        # No-op for non-Slack keys; bot_id passes through for
        # workspace-scoped token routing.
        activity = ua.rewrite_slack_dm_keys(
            activity, network, bot_id=bot_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("user_activity.aggregate failed for %s: %s", bot_id, exc)
        activity = {}
    # Phase F.2 — "seen recently": users who messaged the bot in the
    # last 30 days but aren't in allowFrom or pairing. discover_candidates
    # walks the same turn rollups identity_discovery uses for the
    # Discover-from-history button; resolve_with_names attaches names
    # AND (for Slack) rewrites D-channel ids to U-user ids via F.1.
    # Soft-fails per the same pattern as the activity aggregator.
    seen_by_channel: dict[str, list[dict]] = {}
    bot_user = (network.get("bots", {}).get(bot_id) or {}).get("user") or bot_id
    try:
        from ..evo import identity_discovery as _idsc
        # top_k=50 gives plenty of headroom across all channels of one
        # bot; the per-channel filter trims further. lookback_days
        # matches user_activity's 30-day window for consistency.
        candidates = _idsc.discover_candidates(
            bot_id, bot_user=bot_user, lookback_days=30, top_k=50)
        candidates = _idsc.resolve_with_names(
            network, candidates, bot_id=bot_id)
        for c in candidates:
            seen_by_channel.setdefault(c["channel"], []).append(c)
    except Exception as exc:  # noqa: BLE001
        log.warning("identity_discovery for %s failed: %s", bot_id, exc)
    # R1a (2026-06-17) — the config-level group/channel allowlist
    # (``openclaw.json::channels.<ch>.allowFrom`` under
    # ``groupPolicy: allowlist``). This is a SEPARATE OpenClaw gate from
    # the credentials DM pairing store the ``approved`` list reads, and
    # the two routinely diverge. Surfacing it (a) dissolves the "Active ·
    # not admitted" false negative for group-authorized users and (b)
    # gives the operator visibility into who can drive the bot in
    # channels. Per the R1a decision the two lists are kept strictly
    # separate (no auto cross-population); management of the group list
    # is the R1a follow-up, so this read is render-only.
    # Read openclaw.json once and share it with BOTH group resolvers (the
    # effective lists + the gated-channel set), so the page makes one config
    # read, not two. ``group_gated`` carries channels that are allowlist-gated
    # even when their effective list is empty — the UI needs that to show the
    # add-by-id control on a gated-but-empty channel (R1a PR2).
    try:
        oc_cfg = _read_json_or_none(_openclaw_json_path(network, bot_id))

        def _oc_reader() -> "dict | None":
            return oc_cfg

        group_allowlists = rr.read_group_allowlists(
            network, bot_id, oc_reader=_oc_reader)
        group_gated = rr.group_allowlist_gated_channels(
            network, bot_id, oc_reader=_oc_reader)
    except Exception as exc:  # noqa: BLE001
        log.warning("read_group_allowlists for %s failed: %s", bot_id, exc)
        group_allowlists = {}
        group_gated = set()
    for ch in KNOWN_PROVIDERS:
        pairing_p = _pairing_path(network, bot_id, ch)
        allow_p = _allowfrom_path(network, bot_id, ch)
        pairing_raw = _read_json_or_none(pairing_p)
        allow_raw = _read_json_or_none(allow_p)
        # ``supported`` = at least one of the per-channel files was
        # readable (either via direct ACL or sudo fallback). We can't
        # use Path.exists() here because the credentials/ dir is
        # mode-700 and evolve traversal is blocked on bots whose ACL
        # predates the OC-created subdir — exists() lies in that case.
        supported = pairing_raw is not None or allow_raw is not None
        pairing = pairing_raw if pairing_raw is not None else {"version": 1, "requests": []}
        allow = allow_raw if allow_raw is not None else {"version": 1, "allowFrom": []}
        approved: list[dict] = []
        for ext_id in (allow.get("allowFrom") or []):
            if not ext_id:
                continue
            # The single allowlist+overlay+name+activity join lives in
            # roster_resolver.build_record (spec 2026-06-15 §6 piece 4 /
            # invariant #1). The Users page and the bot projection (F1b)
            # both consume it, so they can't diverge. We map the record to
            # the page's existing approved-entry JSON shape below — no
            # behavior change, just one source for the join.
            rec = rr.build_record(
                network, bot_id, ch, ext_id,
                overlay=overlay, activity=activity, shared_dir=shared_dir)
            entry = _record_to_approved_entry(rec)
            _attach_directory(entry, network, bot_id, ch, ext_id, rec,
                              overlay=overlay, directory=directory,
                              shared_dir=shared_dir)
            approved.append(entry)
        # R1a — the config-level group/channel allowlist for this channel,
        # resolved through the SAME canonical join as ``approved`` so names/
        # roles/activity render identically. Kept as a distinct list (not
        # merged into ``approved``): different OpenClaw gate, different file,
        # and the operator must see DM-paired vs group-authorized as
        # separate facts. The UI distinguishes the two lists structurally
        # (separate arrays); ``access_source: "group_allowlist"`` is carried
        # for any client (or future consumer) that wants to label provenance
        # without inferring it from which array the entry came in.
        group_access: list[dict] = []
        for gid in (group_allowlists.get(ch) or []):
            if not gid:
                continue
            grec = rr.build_record(
                network, bot_id, ch, gid,
                overlay=overlay, activity=activity, shared_dir=shared_dir)
            gentry = _record_to_approved_entry(grec)
            gentry["access_source"] = "group_allowlist"
            _attach_directory(gentry, network, bot_id, ch, gid, grec,
                              overlay=overlay, directory=directory,
                              shared_dir=shared_dir)
            group_access.append(gentry)
        admin_ids = _pod_admin_ids_for(network, ch)
        pending: list[dict] = []
        for req in (pairing.get("requests") or []):
            ext_id = req.get("id")
            if not ext_id:
                continue
            auto_eligible = ext_id in admin_ids
            pending.append({
                "id": ext_id,
                "code": req.get("code"),
                "createdAt": req.get("createdAt"),
                "lastSeenAt": req.get("lastSeenAt"),
                "meta": req.get("meta") or {},
                "auto_approve_eligible": auto_eligible,
                "auto_approve_reason":
                    "known pod admin" if auto_eligible else None,
            })
        channel_settings = ro.channel_block(overlay, ch)
        # Phase F.2 — seen_recently is the set of identities that
        # appear in this bot's recent turn history but aren't in
        # approved, pending, or the overlay block list. Surfaces the
        # "user messaged the bot but never paired" case that
        # previously was visible only behind the Discover button.
        approved_ids = {e["id"] for e in approved}
        # R1a — group-authorized ids are admitted (to channels), so they
        # must NOT show up as "Active · not admitted". This exclusion is
        # the concrete fix for the false negative: a user on the config
        # group allowlist now reads as group-admitted instead.
        group_access_ids = {e["id"] for e in group_access}
        pending_ids = {req["id"] for req in pending if req.get("id")}
        blocked_ids = {
            k.split(":", 1)[1]
            for k in (overlay.get("blocked") or {})
            if k.startswith(f"{ch}:")
        }
        # Operator-dismissed identities (the "Ignore" triage action) are
        # hidden from the seen-recently list but, unlike blocked ones,
        # keep whatever admission they already had.
        ignored_ids = {
            k.split(":", 1)[1]
            for k in (overlay.get("ignored") or {})
            if k.startswith(f"{ch}:")
        }
        seen_recently: list[dict] = []
        for c in seen_by_channel.get(ch, []):
            eid = c.get("external_id")
            if not eid:
                continue
            if (eid in approved_ids or eid in group_access_ids
                    or eid in pending_ids
                    or eid in blocked_ids or eid in ignored_ids):
                continue
            # G.2 — for Slack, only U-prefix and W-prefix are actual
            # user ids. C-/G-prefix are channel ids; lowercase c-/g-
            # are non-standard conversation/thread context ids
            # extracted from session keys like
            # ``agent:main:slack:c08c5n1c3gw:thread:...``. None of
            # those represent a person the operator can pair/block,
            # so they'd just clutter the section with "[unknown]"
            # rows. D-prefix would only reach here if the F.1 rewrite
            # failed (no token / non-IM channel) — same problem; drop
            # them too. The rewrite path preserves via_channel + sets
            # external_id to the U-id, which passes this filter.
            if ch == "slack" and not eid[:1] in ("U", "W"):
                continue
            entry = {
                "id": eid,
                "display_name": c.get("display_name"),
                "username": c.get("username"),
                "turn_count": c.get("turn_count", 0),
                "first_seen": c.get("first_seen"),
                "last_seen": c.get("last_seen"),
            }
            # F.1 stamps via_channel when the original turn-record id
            # was a Slack D-channel that we rewrote to a U-user. Keep
            # it on the entry for traceability — the UI shows it as a
            # subtle hover hint so operators can spot DM-derived rows.
            if c.get("via_channel"):
                entry["via_channel"] = c["via_channel"]
            seen_recently.append(entry)
        out[ch] = {
            "supported": supported,
            "approved": approved,
            # R1a — config-level group/channel allowlist (distinct gate +
            # file from the DM pairing store the ``approved`` list reads).
            # Empty list for channels with no allowlist-gated group access.
            "group_access": group_access,
            # R1a PR2 — True iff this channel is group-allowlist-gated
            # (groupPolicy == "allowlist"), even when ``group_access`` is empty.
            # Drives the management UI: a gated-but-empty channel still shows the
            # add-by-id control so the operator can authorize the first member.
            "group_allowlist_gated": ch in group_gated,
            "pending": pending,
            "seen_recently": seen_recently,
            # Per-channel overlay settings (spec 2026-06-07 §11).
            # newcomer_mode defaults to 'require_approval' — the existing
            # 2026-05-29 behavior — so an unconfigured channel admits via
            # the pairing flow with no behavior change.
            "newcomer_mode": channel_settings["newcomer_mode"],
            "default_engagement_surfaces":
                channel_settings["default_engagement_surfaces"],
        }
    return out


def _record_to_approved_entry(rec: rr.RosterRecord) -> dict[str, Any]:
    """Map a canonical ``RosterRecord`` onto the Users-page approved-entry JSON.

    Key set + insertion order are preserved exactly from the pre-resolver code so the
    GET response is byte-identical: ``id, display_name, labels, source, role,
    engagement_surfaces`` always; ``username`` / ``email`` only when present;
    ``last_seen`` + ``turns_7d`` only when there is an activity record (``turn_count is
    not None`` ⇔ the old ``if act:`` gate, since ``turns_7d`` is always an int — possibly
    0 — when activity exists). The record's bot-only fields (``rights_summary``) are
    deliberately not emitted here — adding them would change the page JSON; they're for
    the F1b bot projection.
    """
    entry: dict[str, Any] = {
        "id": rec.stable_id,
        "display_name": rec.display_name,
        "labels": rec.labels,
        "source": rec.name_source,
        "role": rec.role,
        "engagement_surfaces": rec.engagement_surfaces,
    }
    if rec.handle:
        entry["username"] = rec.handle
    if rec.email:
        entry["email"] = rec.email
    if rec.turn_count is not None:
        entry["last_seen"] = rec.last_seen
        entry["turns_7d"] = rec.turn_count
    return entry


def _attach_directory(
    entry: dict[str, Any],
    network: dict,
    bot_id: str,
    platform: str,
    stable_id: str,
    rec: rr.RosterRecord,
    *,
    overlay: dict,
    directory: dict,
    shared_dir: Path,
) -> None:
    """Additively attach the directory-owned ``Person`` fields to an approved entry.

    Realizes spec-user-directory-2026-06-22 §10 invariant #1 ("one read path"): the
    page reads directory-owned fields **only** through ``resolve_person``, never the
    store file directly. The fields land under a nested ``directory`` key — purely
    additive, so every existing key/consumer (``web/.../users.js``) is untouched and the
    change is trivially reversible.

    ``rec`` is the identity's already-built admitted ``RosterRecord``; passing it as a
    single-record ``roster`` keeps ``membership`` authoritative (this id IS admitted —
    it came from the allowFrom / group allowlist) without a second roster read. In
    Phase 1 the directory store is unwritten, so ``emails`` / ``contact`` /
    ``identities`` are empty and only the stable ``person_id`` is populated — the seam
    is live and the payload is honest about having no contact data yet. Soft-fails: a
    resolver error logs and leaves the entry exactly as it was (the directory block is
    simply absent), never 500-ing the page.
    """
    try:
        person = udr.resolve_person(
            network, bot_id, (platform, stable_id),
            shared_dir=shared_dir, overlay=overlay, directory=directory,
            roster=[rec])
        if person is None:
            return
        entry["directory"] = {
            "person_id": person.person_id,
            "emails": [e.to_dict() for e in person.emails],
            "contact": dict(person.contact),
            # Directory-ADDED identities only — the base channel identity is already
            # carried by the entry's id/username fields.
            "identities": [
                i.to_dict() for i in person.identities
                if not (i.platform == platform and i.id == stable_id)
            ],
            "profile_ref": person.profile_ref,
        }
    except Exception as exc:  # noqa: BLE001 — directory enrichment is best-effort
        log.warning("resolve_person for %s/%s:%s failed: %s",
                    bot_id, platform, stable_id, exc)


def _blocked_list(overlay: dict) -> list[dict]:
    """Render the overlay's sticky-block index as a list for the UI.

    Each entry is a flat record the UI can iterate without parsing the
    ``"<platform>:<id>"`` composite key. Sorted by ``blocked_at``
    descending (most-recent blocks first) so the operator sees recent
    actions at the top.
    """
    items: list[dict] = []
    for key, rec in (overlay.get("blocked") or {}).items():
        if ":" not in key:
            continue  # malformed key — skip silently
        platform, _, stable_id = key.partition(":")
        items.append({
            "platform": platform,
            "id": stable_id,
            "blocked_at": rec.get("blocked_at"),
            "blocked_by": rec.get("blocked_by"),
            "reason": rec.get("reason", ""),
        })
    items.sort(key=lambda x: x.get("blocked_at") or "", reverse=True)
    return items


def _shared_dir_from_net(network: dict) -> Path:
    return Path(network.get("sharedDir") or "/Users/Shared/evolve")


def _tier_pref_rows(shared_dir: Path, bot_id: str) -> list[dict[str, Any]]:
    """Render user-tier-prefs.json entries as UI-ready rows.

    Each row: ``{user_key, default_role, updated_at, channel?,
    external_id?}``. The ``ext:<channel>:<external_id>`` key shape (from
    ``evo.identity.derive_user_key``) is parsed so the frontend can join
    against the by_channel identity lists for a friendly name; ``pod:`` /
    ``anon:`` keys pass through with no channel fields and render as-is.
    ``default_role`` honours the legacy ``defaultTier`` fallback via
    ``list_user_prefs``'s entry filter, mirroring the plugin reader.
    Sorted by user_key for a stable render order.
    """
    from ..evo.user_tier_prefs import list_user_prefs

    rows: list[dict[str, Any]] = []
    for user_key, entry in sorted(list_user_prefs(shared_dir, bot_id).items()):
        role = entry.get("defaultRole")
        if not isinstance(role, str):
            role = entry.get("defaultTier")
        if not isinstance(role, str):  # pragma: no cover — filtered upstream
            continue
        row: dict[str, Any] = {
            "user_key": user_key,
            "default_role": role.strip().lower(),
            "updated_at": entry.get("updated_at"),
        }
        if user_key.startswith("ext:"):
            parts = user_key.split(":", 2)
            if len(parts) == 3 and parts[1] and parts[2]:
                row["channel"] = parts[1]
                row["external_id"] = parts[2]
        rows.append(row)
    return rows


# ── Phase 2: channel-API name enrichment ─────────────────────────────────


def _enrich_unknown_names(network: dict, by_channel: dict[str, dict],
                         max_seconds: float = 5.0,
                         bot_id: "str | None" = None) -> bool:
    """Resolve display names for approved IDs that lack one.

    Iterates ``by_channel.<ch>.approved`` for channels that the
    ``name_resolver`` module supports (telegram, slack). For each entry
    with ``display_name=None``, fires a concurrent call to the channel
    API via ``name_resolver.resolve(use_cache=False)`` — bypassing the
    cache because we already checked it during ``_resolve_display_name``
    and got nothing. Mutates entries in-place with discovered names
    and updates ``source`` to ``"channel_api"``.

    Capped at ``max_seconds`` wall-clock; stragglers don't block the
    response (they may still write to the cache mid-flight, but their
    results won't appear until the next GET). Returns True iff at least
    one ID was successfully resolved — caller should ``save_network`` to
    persist the cache writes that name_resolver made.

    Channels not in ``SUPPORTED_CHANNELS`` (discord, whatsapp) are
    skipped; their IDs stay as bare-ID rows until those channels get
    resolve() coverage.
    """
    import time
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed

    from ..evo import name_resolver as nr

    # Build the work list — only IDs without a name on supported channels.
    # Covers both the DM ``approved`` list and the R1a ``group_access``
    # list; group-authorized users typically never paired, so they're the
    # most likely to lack a cached name and benefit most from the live
    # channel-API lookup.
    work: list[tuple[str, str, dict]] = []
    for ch, ch_data in by_channel.items():
        if ch not in nr.SUPPORTED_CHANNELS:
            continue
        for list_key in ("approved", "group_access"):
            for entry in (ch_data.get(list_key) or []):
                if entry.get("display_name"):
                    continue
                ext_id = entry.get("id")
                if not ext_id:
                    continue
                work.append((ch, ext_id, entry))

    if not work:
        return False

    any_resolved = False
    deadline = time.monotonic() + max_seconds
    # max_workers=8: a typical pod has 1-11 unknown IDs per channel;
    # 8 keeps Slack's rate-limit headroom without saturating.
    ex = ThreadPoolExecutor(max_workers=8, thread_name_prefix="name-enrich")
    try:
        futures = {
            # Pass bot_id so name_resolver picks THIS bot's channel
            # token first. Critical for Slack (workspace-scoped tokens)
            # where the previous "first bot's token wins" path silently
            # failed for cross-workspace bots — see name_resolver
            # _channel_token docstring.
            ex.submit(nr.resolve, network,
                      channel=ch, external_id=ext_id, use_cache=False,
                      bot_id=bot_id): entry
            for (ch, ext_id, entry) in work
        }
        try:
            for fut in as_completed(
                futures, timeout=max(0.1, deadline - time.monotonic()),
            ):
                entry = futures[fut]
                try:
                    resolved = fut.result(timeout=0)
                except Exception:  # noqa: BLE001
                    resolved = None
                if resolved and resolved.get("name"):
                    entry["display_name"] = resolved["name"]
                    entry["source"] = "channel_api"
                    any_resolved = True
        except FuturesTimeout:
            log.debug(
                "name enrichment hit budget %ss with stragglers "
                "(they may still update cache)", max_seconds,
            )
    finally:
        # Don't block on stragglers — let them complete in the
        # background. Their cache writes will appear in the next GET.
        ex.shutdown(wait=False)
    return any_resolved


def _pod_admin_ids_for(network: dict, channel: str) -> set[str]:
    """Every pod-admin id on ``channel``. Both external_ids shapes (M1-B2)."""
    pod = network.get("pod") or {}
    return set(_external_ids.ids_for(pod.get("admins"), channel))



# ── Name resolution + labels ─────────────────────────────────────────────
#
# The display-name / username / email / label join moved to
# ``roster_resolver`` so the single canonical resolver owns it (spec
# 2026-06-15 §6 piece 4). ``_meta_to_display_name`` and
# ``_identity_cache_path`` are re-exported as module aliases below because
# the pairing-time identity-cache *write* (``_write_identity_cache``, which
# stays here) and the existing tests reference them as ``rbu`` attributes.
_meta_to_display_name = rr.meta_to_display_name
_identity_cache_path = rr.identity_cache_path


# ── Identity cache (pairing-time meta) ───────────────────────────────────


def _write_identity_cache(shared_dir: Path, channel: str, ext_id: str,
                          meta: dict) -> None:
    """Persist pairing meta so future GETs can render the user's name.

    ``shared_dir`` is owned by the ``evolve`` user, so this is a direct
    write — no /tmp staging or sudo needed. The file is world-readable
    (mode 644) because nothing in it is secret; the meta carries names
    and usernames the channel already revealed to the bot.
    """
    import shutil

    if not isinstance(meta, dict) or not meta:
        return
    target = _identity_cache_path(shared_dir, channel, ext_id)
    payload = {
        "version": 1,
        "channel": channel,
        "id": ext_id,
        "display_name": _meta_to_display_name(channel, meta),
        "meta": meta,
        "source": "pairing_meta",
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError) as e:
        log.warning("identity_cache mkdir for %s/%s failed: %s",
                    channel, ext_id, e)
        return
    fd, tmp = tempfile.mkstemp(
        dir="/tmp", prefix="evolve-idcache-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
        try:
            shutil.copy2(tmp, target)
            os.chmod(target, 0o644)
        except (PermissionError, OSError) as e:
            log.warning("identity_cache write for %s/%s failed: %s",
                        channel, ext_id, e)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ── Write side ───────────────────────────────────────────────────────────


def _write_bot_json(path: Path, payload: dict, *, bot_user: str) -> None:
    """Atomic write of bot-owned JSON via ``/tmp`` staging.

    Tries a direct ``shutil.copy2`` first (works in tests and on any path
    the calling process already owns), falling back to
    ``sudo <cp>`` + ``sudo <chown> <bot_user>:staff`` +
    ``sudo <chmod> 600`` for production bot-owned paths under
    ``/Users/<bot>/.openclaw/credentials/``. The cp/chown/chmod binaries
    are routed through ``platform_profile.get_profile()`` so the INVOKED
    path always matches the path the evolve sudoers grant was rendered
    with (both derive from the same profile) — on Linux ``chown`` is
    ``/usr/bin/chown``, not the macOS ``/usr/sbin/chown``; a hardcoded
    macOS path is absent from the Linux NOPASSWD allowlist, so sudo would
    fall through to a password prompt and the TTY-less admin daemon fails
    with "sudo: a terminal is required" — silently blocking the
    approve/revoke write. The sudoers grants for this fallback are added by
    ``setup_wizard._render_evolve_sudoers``. Same pattern as
    ``config.save_network`` and ``_oc_install_common``.

    The chown step is load-bearing: without it, ``sudo /bin/cp`` writes
    the file owned by root (mode 600 → unreadable by the bot user), and
    the OC gateway running as the bot user hits EACCES on its own
    credentials file the next time it handles a message. Symptom:
    ``TelegramPairingStoreReadError: EACCES`` and the user sees
    "⚠️ Couldn't process this message" in their DM.

    Args:
      path:     Target file under ``/Users/<bot_user>/.openclaw/credentials/``.
      payload:  Dict to serialize as JSON.
      bot_user: macOS username that the OC gateway runs as
                (e.g. ``atlas``, not always == ``bot_id``).
    """
    import shutil

    text = json.dumps(payload, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir="/tmp", prefix="evolve-pairing-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        try:
            shutil.copy2(tmp, path)
            os.chmod(path, 0o600)
        except PermissionError:
            prof = _get_profile()
            try:
                subprocess.run(
                    ["sudo", prof.cp, tmp, str(path)],
                    check=True, capture_output=True, text=True,
                )
                # Hand the file back to the bot user BEFORE chmod 600 —
                # otherwise root-owned-mode-600 is unreadable to the bot
                # process and OC's pairing-store read fails EACCES.
                subprocess.run(
                    ["sudo", prof.chown,
                     f"{bot_user}:staff", str(path)],
                    check=True, capture_output=True, text=True,
                )
                subprocess.run(
                    ["sudo", prof.chmod, "600", str(path)],
                    check=True, capture_output=True, text=True,
                )
            except subprocess.CalledProcessError as e:
                msg = (e.stderr or "").strip() or str(e)
                raise _PairingError(
                    f"write {path.name} failed: {msg}") from e
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _approve(network: dict, bot_id: str, channel: str,
             ext_id: str, code: str | None) -> None:
    """Add ``ext_id`` to allowFrom; remove matching request from pairing.json.

    Before dropping the pending request, capture any pairing-time meta
    (name, username, etc.) into the identity cache so subsequent GETs
    can still render the user's display name — OC's ``allowFrom`` is a
    bare list of IDs and would otherwise lose all human-readable info.

    **D1 consult (M1-B2b).** This is the admission write: after it, ``ext_id``
    is a roster row. Before writing, we ask ``roster_identity`` whether that
    admission key already indexes into an existing person
    (``bots.<bot>.primary_user`` or the pod-admin bag). Two outcomes, both
    correct, neither a guess:

    * **resolves** → the id belongs to a person we already have a row for, so
      admitting it adds a *key*, not a person. Nothing extra is written: D1 is
      already satisfied by the row that holds this id, and a projection can
      collapse both platforms onto one person via
      ``roster_identity.person_key_for``.
    * **unknown** → a stranger, and a stranger IS a new person until an
      operator says otherwise. Admission proceeds and the id becomes a new row.
      We deliberately do NOT try to match them to an existing person by name or
      handle — roles attach to admitted identities, so a wrong link is a
      privilege transfer. Asserting the link is the operator's explicit call
      (``roster_identity.link_external_id``, surfaced by M1-B4).

    Recorded at INFO either way so the audit trail says which of the two
    happened at the moment of admission.
    """
    # G.5 — Slack user ids start with U (modern) or W (legacy). Any
    # other prefix (C/G channel ids, lowercase c/g thread contexts,
    # D IM channel ids) is not a user and shouldn't end up in
    # allowFrom. G.2 filters these out of Seen Recently DISPLAY but
    # the action endpoint is a separate gate — without this check an
    # operator who approved a noise row before G.2 deployed would
    # leave a useless "[unknown]" entry stuck in the approved list.
    # Doesn't apply to revoke/reject — those need to be able to clean
    # up existing bad entries.
    if channel == "slack" and ext_id and ext_id[0] not in ("U", "W"):
        raise _PairingError(
            f"refusing to approve slack id {ext_id!r}: not a user id "
            f"(Slack user ids start with U or W; this looks like a "
            f"channel or thread-context id)"
        )
    log.info(
        "admission %s/%s: %s",
        bot_id, channel,
        ri.resolve_admission(network, bot_id, channel, ext_id).describe(),
    )
    pp = _pairing_path(network, bot_id, channel)
    ap = _allowfrom_path(network, bot_id, channel)
    pairing = _read_json_or_default(pp, {"version": 1, "requests": []})
    allow = _read_json_or_default(ap, {"version": 1, "allowFrom": []})
    # Capture meta from any matching pending request BEFORE we drop it.
    # We use the most-recent (last) match because OC's pairing.json may
    # carry multiple stale codes for the same ID; the latest meta is
    # usually the freshest data.
    pending_match: "dict | None" = None
    for req in (pairing.get("requests") or []):
        if req.get("id") == ext_id:
            pending_match = req
    if pending_match and pending_match.get("meta"):
        _write_identity_cache(
            _shared_dir_from_net(network), channel, ext_id,
            pending_match["meta"],
        )
    # Drop ALL pairing entries for this ID (stale codes get cleaned up too).
    pairing["requests"] = [
        r for r in (pairing.get("requests") or [])
        if r.get("id") != ext_id
    ]
    allowed = list(allow.get("allowFrom") or [])
    if ext_id not in allowed:
        allowed.append(ext_id)
    allow["allowFrom"] = allowed
    # Write allowFrom first so a partial failure leaves the user
    # already-allowed but with a stale pending entry, which is the safer
    # half-state (admin can still re-reject later).
    bot_user = get_bot_user(bot_id, network)
    _write_bot_json(ap, allow, bot_user=bot_user)
    _write_bot_json(pp, pairing, bot_user=bot_user)


def _revoke(network: dict, bot_id: str, channel: str,
            ext_id: str, code: str | None) -> None:
    """Remove ``ext_id`` from allowFrom.json. Idempotent."""
    ap = _allowfrom_path(network, bot_id, channel)
    allow = _read_json_or_default(ap, {"version": 1, "allowFrom": []})
    allow["allowFrom"] = [
        x for x in (allow.get("allowFrom") or []) if x != ext_id
    ]
    _write_bot_json(ap, allow, bot_user=get_bot_user(bot_id, network))


def _reject(network: dict, bot_id: str, channel: str,
            ext_id: str, code: str | None) -> None:
    """Remove matching request(s) from pairing.json without allowlisting."""
    pp = _pairing_path(network, bot_id, channel)
    pairing = _read_json_or_default(pp, {"version": 1, "requests": []})
    reqs = pairing.get("requests") or []
    if code:
        pairing["requests"] = [
            r for r in reqs
            if not (r.get("id") == ext_id and r.get("code") == code)
        ]
    else:
        pairing["requests"] = [r for r in reqs if r.get("id") != ext_id]
    _write_bot_json(pp, pairing, bot_user=get_bot_user(bot_id, network))


# ── Group/channel allowlist write side (R1a PR2) ─────────────────────────
#
# These manage the config-level group allowlist
# (``openclaw.json::channels.<ch>``) — a SEPARATE OpenClaw gate from the
# credentials DM pairing store the approve/revoke helpers above write. Three
# pieces, smallest-blast-radius first:
#   * ``_group_allowlist_target_key``  — which key holds the EFFECTIVE list,
#   * ``_apply_group_allowlist_change`` — a pure read-modify-write on the
#     parsed config dict (no I/O — unit-testable in isolation),
#   * ``_group_allowlist_write``        — the orchestration: read openclaw.json,
#     apply, persist via the schema-validating writer, kick the gateway.


def _group_allowlist_target_key(block: dict) -> str:
    """The ``channels.<ch>`` key that holds the EFFECTIVE group allowlist.

    Mirrors the read-side fall-through in
    ``roster_resolver.effective_group_allowlist`` so a write lands in the same
    key the resolver reads — otherwise an edit would be invisible to the GET
    (requirement: one source of truth). Order:

      1. ``groupAllowFrom`` when present as a list — the channel's own group
         list takes precedence (and OpenClaw ignores ``allowFrom`` for groups
         once it is set), so that is what must be mutated.
      2. ``allowFrom`` when ``groupAllowFromFallbackToAllowFrom`` is not
         ``false`` (OpenClaw's documented default is ``true``) — the
         R1a-diagnosed live-pod shape: no separate ``groupAllowFrom``, so the
         group gate IS ``channels.<ch>.allowFrom``.
      3. ``groupAllowFrom`` otherwise (fallback explicitly disabled, no list
         yet) — ``allowFrom`` is not consulted for groups, so a first approve
         must create ``groupAllowFrom``.

    Note this is ``channels.<ch>.allowFrom`` inside openclaw.json — NOT the
    credentials ``<ch>-default-allowFrom.json`` DM pairing store (a different
    file). Mutating it never touches DM admission.
    """
    if isinstance(block.get("groupAllowFrom"), list):
        return "groupAllowFrom"
    if block.get("groupAllowFromFallbackToAllowFrom", True):
        return "allowFrom"
    return "groupAllowFrom"


def _apply_group_allowlist_change(
    oc_config: dict, channel: str, ext_id: str, *, add: bool,
) -> "tuple[dict, str] | None":
    """Pure read-modify-write of the group allowlist on a parsed config.

    Returns ``(new_config, target_key)`` with ``ext_id`` added/removed from the
    channel's effective group-allowlist key, or ``None`` when the action is a
    no-op (already present on approve / already absent on revoke) — the caller
    then skips the write AND the disruptive gateway restart entirely.

    Deep-copies the input so the caller's parsed config is never mutated in
    place (a half-applied dict must never reach disk). Only the target key under
    ``channels.<ch>`` is touched: every sibling channel, every other key on this
    channel (``groupPolicy``, ``requireMention``, tokens, …), and the rest of
    the config are preserved verbatim.

    Raises ``_PairingError`` (→ 400) when there is no managed list to mutate:
    no ``channels.<ch>`` block, or ``groupPolicy != "allowlist"`` (``open``
    admits everyone, ``disabled`` admits no one — neither is an allowlist the
    operator curates). Fails loudly rather than silently inventing config.
    """
    cfg = copy.deepcopy(oc_config) if isinstance(oc_config, dict) else {}
    channels = cfg.get("channels")
    if not isinstance(channels, dict) or not isinstance(
            channels.get(channel), dict):
        raise _PairingError(
            f"no openclaw.json channels.{channel} block for this bot — group "
            f"access can't be managed until the channel has an allowlist "
            f"(configure groupPolicy first)")
    block = channels[channel]
    if block.get("groupPolicy") != "allowlist":
        raise _PairingError(
            f"group access for {channel} is not allowlist-gated "
            f"(groupPolicy={block.get('groupPolicy')!r}); approve/revoke "
            f"manages an allowlist only")
    # Slack user ids start with U (modern) or W (legacy). Refuse to allowlist a
    # channel/thread-context id (C/G/D/lowercase) — same guard as DM _approve;
    # a bad id is never legitimately present, so guard before the membership
    # check. Doesn't apply to revoke (must be able to clean up a bad entry).
    if add and channel == "slack" and ext_id[:1] not in ("U", "W"):
        raise _PairingError(
            f"refusing to add slack id {ext_id!r} to the group allowlist: "
            f"not a user id (Slack user ids start with U or W; this looks "
            f"like a channel or thread-context id)")
    key = _group_allowlist_target_key(block)
    raw = block.get(key)
    current = [x for x in raw if isinstance(x, str) and x] \
        if isinstance(raw, list) else []
    present = ext_id in current
    if add and present:
        return None   # idempotent: already authorized → no write, no restart
    if not add and not present:
        return None   # idempotent: already absent → no write, no restart
    block[key] = (current + [ext_id]) if add \
        else [x for x in current if x != ext_id]
    return cfg, key


def _group_allowlist_write(
    network: dict, bot_id: str, channel: str, ext_id: str, *, add: bool,
) -> "str | None":
    """Read openclaw.json, apply the group-allowlist change, persist + reload.

    Returns ``None`` on a clean apply (or idempotent no-op), or a warning
    string when the write landed but the gateway restart failed. Raises
    ``_PairingError`` (bad request) / ``_GroupWriteError`` (write failed).

    The write goes through ``deploy.safe_write_bot_config`` — the SAME
    schema-validating path every other openclaw.json writer uses. It stages to
    /tmp, validates the merged config against the OC schema (an invalid
    openclaw.json crash-loops the gateway — so a blind ``sudo cp`` is never
    acceptable here), backs up to ``openclaw.json.bak``, ``sudo /bin/cp``s,
    chowns back to the bot, and chmods 0600 (token-bearing). All grants exist
    today (sudoers §1/§4/§6/§13) — no sudoers change.

    Propagation: OpenClaw reads ``channels.*`` at gateway startup and does NOT
    hot-reload openclaw.json (unlike the credentials DM store, which it reads
    per-message). So a gateway kick is required for the new allowlist to take
    effect — mirroring every other openclaw.json writer (memory-slot toggle,
    plugin pin, the arbiter appliers). A kick failure is surfaced, not rolled
    back: the config is durable and the operator can restart manually.
    """
    oc_path = _openclaw_json_path(network, bot_id)
    current = _read_json_or_none(oc_path)
    if current is None:
        # File genuinely unreachable even via the sudo /bin/cat fallback — fail
        # loudly rather than fabricate a config from scratch (which would
        # clobber the bot's real openclaw.json).
        raise _GroupWriteError(
            f"could not read {bot_id}'s openclaw.json — refusing to write "
            f"(would risk clobbering live config)")
    result = _apply_group_allowlist_change(current, channel, ext_id, add=add)
    if result is None:
        return None   # idempotent no-op — nothing to write or restart
    new_cfg, key = result
    bot_user = get_bot_user(bot_id, network)
    action = "approve" if add else "revoke"
    reason = (f"group-allowlist {action} {channel}:{ext_id} "
              f"(channels.{channel}.{key}) by {_requester_label()}")
    # Lazy import keeps the heavy deploy module out of this module's load-time
    # graph and lets the test suite patch the seam (the established pattern for
    # admin-runtime openclaw.json writes — see admin_bot_routes).
    try:
        from ..deploy import restart_gateway, safe_write_bot_config
    except ImportError as exc:  # pragma: no cover - deploy always present in prod
        raise _GroupWriteError(f"deploy module unavailable: {exc}") from exc
    ok, err = safe_write_bot_config(
        bot_id, new_cfg, reason=reason, bot_user=bot_user)
    if not ok:
        raise _GroupWriteError(f"openclaw.json write failed: {err}")
    # Record the expected-state baseline so the out-of-band drift monitor
    # (roster_allowlist_drift_monitor) does not read this admin-made change as
    # drift. The config is already durable; baseline recording is a best-effort
    # safety-net write and must NEVER fail the confirmed allowlist write. Uses
    # ``new_cfg`` (what we just persisted) so the snapshot matches disk with no
    # re-read/race. R1a PR3 — see evolve_admin.roster_baseline.
    try:
        from ..roster_baseline import record_baseline_from_config
        record_baseline_from_config(network, bot_id, new_cfg)
    except Exception as exc:  # noqa: BLE001 — write landed; baseline is best-effort
        log.warning("group-allowlist %s for %s/%s: baseline record failed: %s",
                    action, bot_id, channel, exc)
    try:
        restart_gateway(bot_id, bot_user=bot_user)
    except Exception as exc:  # noqa: BLE001 — write landed; restart is best-effort
        log.warning("group-allowlist %s for %s/%s: gateway restart failed: %s",
                    action, bot_id, channel, exc)
        return (f"Group allowlist updated, but the {bot_id} gateway restart "
                f"failed ({type(exc).__name__}). The change is saved; restart "
                f"the gateway to apply it.")
    return None


# Sentinel: an X-Requester-Identity header that is PRESENT but unparseable.
# Distinct from ``None`` (header absent) so ``_check_capability`` can deny it
# outright — a caller that tried to assert an identity and failed must never
# be coerced onto the header-less trusted-UI path (WO-H1-2 / CBR-1.2).
# A single-member Enum (not a bare ``object()``) so ``is _MALFORMED_IDENTITY``
# narrows the union for the type checker at each consumer.
class _MalformedIdentity(enum.Enum):
    TOKEN = enum.auto()


_MALFORMED_IDENTITY = _MalformedIdentity.TOKEN


def _parse_requester_identity() -> (
        "tuple[str, str, str | None] | None | _MalformedIdentity"):
    """Read the ``X-Requester-Identity`` header into ``(platform, stable_id, source_bot)``.

    Header shape: ``<platform>:<stable_id>`` (e.g. ``telegram:1260193629``).
    Optional companion header ``X-Requester-Source-Bot`` identifies the
    bot whose gateway is making the call on behalf of the requester
    (e.g. evo's gateway calling the daemon on behalf of an admin
    chatting with evo).

    Returns ``None`` when the header is absent (an empty/whitespace-only
    value counts as absent — proxies can strip a header to empty, and an
    empty value asserts nothing). A header-less request is trusted
    **only** when it positively arrived via the authenticated admin-UI
    HTTP transport (``_is_authenticated_ui_request``); a header-less
    caller on the admin unix socket (a bot gateway) is DENIED — the
    header is self-asserted, so its absence is not proof of the UI
    (fail-CLOSED backstop, audit G-N3). See ``_check_capability``.

    Returns ``_MALFORMED_IDENTITY`` when the header is present but does
    not parse (no colon, or empty platform / stable_id segment). This is
    NOT folded into the absent case: the legitimate header-less client
    (the admin UI) never sends the header at all, and both gateway-side
    senders (plugin ``RosterTools.buildRequesterHeaders``, evo
    ``action_roster._requester_headers_or_refusal``) construct it from
    non-empty parts or refuse locally — so a malformed value is never a
    well-behaved caller, and ``_check_capability`` denies it on every
    transport rather than silently escalating it to trusted-UI.
    """
    raw = (request.headers.get("X-Requester-Identity") or "").strip()
    if not raw:
        return None
    if ":" not in raw:
        return _MALFORMED_IDENTITY
    platform, _, stable_id = raw.partition(":")
    platform = platform.strip().lower()
    stable_id = stable_id.strip()
    if not platform or not stable_id:
        return _MALFORMED_IDENTITY
    source_bot = (request.headers.get("X-Requester-Source-Bot") or "").strip() or None
    return (platform, stable_id, source_bot)


def _requester_label() -> str:
    """Audit-log attribution string.

    Three forms today:
      - ``ui:admin`` — no requester header, admin UI is the trusted client
        (the existing path before Phase C).
      - ``<platform>:<stable_id>`` — gateway-attested requester identity
        from the X-Requester-Identity header.
      - ``<platform>:<stable_id> via <source_bot>`` — same with the
        source bot named, for cross-bot calls (e.g. evo relaying for
        the admin).

    Used as the ``by`` field in roster mutation audit log entries so the
    ledger reflects who actually triggered each change.
    """
    req = _parse_requester_identity()
    if req is _MALFORMED_IDENTITY:
        # Unreachable after a passed capability gate (malformed is denied
        # there), but never attribute a malformed assertion to ui:admin.
        # Deliberately colon-free so no parseable header value can collide
        # with this marker in the audit log's <platform>:<id> namespace.
        return "malformed-header"
    if not req:
        return "ui:admin"
    platform, stable_id, source_bot = req
    if source_bot:
        return f"{platform}:{stable_id} via {source_bot}"
    return f"{platform}:{stable_id}"


def _is_authenticated_ui_request(net: dict) -> bool:
    """Positive check: did this request arrive via the authenticated admin-UI
    HTTP (TCP loopback) transport — NOT the unix socket?

    The admin UI is the trusted header-less client: its own device-cookie /
    loopback web auth stands in for a requester identity. A unix-socket caller
    (a bot gateway, reached via ``adminSocket.ts``) is NEVER the UI — its
    identity is self-asserted through ``X-Requester-Identity``, so the
    *absence* of that header must not be read as "trusted admin". This is the
    defense-in-depth backstop for tools that reach the admin socket (G-N3);
    it fails **CLOSED**: anything not provably the authenticated UI → False.

    Two things must hold for a header-less request to be trusted:
      1. Transport is NOT the unix socket. ``REMOTE_TRANSPORT`` is set to
         ``"unix-socket"`` by the socket binding (see
         ``peer_auth._request_transport``); its absence implies the TCP/loopback
         admin-UI binding. A socket request is bot-gateway traffic — return
         False so it must present a header or be denied.
      2. The TCP request is actually authenticated as the UI — either
         device-auth is not enforced (operator opt-out / fresh pod runs open,
         the long-standing "admin UI is the implicit auth layer" model), or a
         valid paired device cookie (``evolve_device``) is present. This mirrors
         ``server._enforce_device_auth`` so the check is self-sufficient and
         does not merely assume the before_request gate ran.
    """
    if (request.environ.get("REMOTE_TRANSPORT") or "tcp") == "unix-socket":
        return False
    try:
        shared = _shared_dir_from_net(net)
        if not admin_auth.is_auth_enabled(shared):
            return True
        return admin_auth.verify_device_token(
            shared, request.cookies.get(admin_auth.DEVICE_COOKIE_NAME))
    except Exception:  # noqa: BLE001 — fail-CLOSED on any resolution error
        return False


def _check_capability(net: dict, bot_id: str,
                      required_capability: str) -> "tuple[bool, str | None]":
    """Resolve the requester's role on ``bot_id`` and check the capability.

    Returns ``(ok, reason)``. When the X-Requester-Identity header is
    absent, the request is trusted ``(True, None)`` **only** when it
    positively arrived via the authenticated admin-UI HTTP transport
    (``_is_authenticated_ui_request``). A caller on the admin **unix socket**
    (a bot gateway) that merely OMITS the header is NOT trusted — the header
    is self-asserted, so its absence is not proof of the UI. Such a caller
    (and any unauthenticated caller) is **denied** ``(False, "<reason>")``.
    This closes the former fail-OPEN "header absent ⇒ trusted UI" assumption
    (audit G-N3): the daemon capability check is the defense-in-depth backstop
    for tools that reach the socket, and a backstop must not fail open.

    A header that is present but MALFORMED (``_MALFORMED_IDENTITY``) is denied
    on every transport, including the authenticated UI one: the caller tried
    to assert an identity and failed, and no legitimate caller sends a
    malformed header (the UI sends none; the gateway senders build it from
    non-empty parts or refuse locally). Folding malformed into "absent" would
    silently escalate a bad identity assertion to trusted-UI (WO-H1-2).

    When the header IS present:
      - Resolve the role on the target bot via ``roster_overlay.resolve_role``.
        Pod-admin claims and overlay state both contribute.
      - Look up the role's capabilities via ``capabilities.resolve_role_capabilities``,
        honoring the per-bot ``role_bindings`` overlay block if set.
      - Return ``(True, None)`` if the capability is present, else
        ``(False, "<reason>")`` for a 403 response body.
    """
    req = _parse_requester_identity()
    if req is _MALFORMED_IDENTITY:
        # Header PRESENT but unparseable — denied on EVERY transport. An
        # asserted-but-broken identity must never fall through to the
        # trusted-UI path (fail-CLOSED, WO-H1-2 / CBR-1.2).
        return False, (
            f"malformed X-Requester-Identity header: capability-gated action "
            f"on bot {bot_id!r} asserted an identity that does not parse as "
            f"<platform>:<stable_id>"
        )
    if not req:
        # Header ABSENT — trusted only for the authenticated admin UI; a
        # socket/unauthenticated caller is denied (fail-CLOSED).
        if _is_authenticated_ui_request(net):
            return True, None  # authenticated admin UI — trusted client
        return False, (
            f"no requester identity: capability-gated action on bot {bot_id!r} "
            f"reached without an X-Requester-Identity header over a non-UI "
            f"transport (unix socket or unauthenticated caller)"
        )
    platform, stable_id, _ = req

    shared = _shared_dir_from_net(net)
    overlay = ro.load_overlay(shared, bot_id)
    role = ro.resolve_role(overlay, net, bot_id, platform, stable_id)
    granted = cap.resolve_role_capabilities(
        role,
        overlay_role_bindings=overlay.get("role_bindings") or {},
        # app_capabilities=None — built-in only until app-cap registry ships
    )
    if required_capability in granted:
        return True, None
    return False, (
        f"role {role!r} on bot {bot_id!r} does not grant {required_capability!r} "
        f"(requester {platform}:{stable_id})"
    )


def _handle_overlay_patch(network_path: Path, bot_id: str,
                          channel: str, ext_id: str) -> Any:
    """PATCH role / engagement_surfaces / notes on an identity."""
    body = request.get_json(silent=True) or {}
    if channel not in KNOWN_PROVIDERS:
        return jsonify({"error": "invalid 'channel'"}), 400
    net = load_network(network_path)
    if not _bot_exists(net, bot_id):
        return jsonify({"error": f"unknown bot: {bot_id}"}), 404
    ok, reason = _check_capability(net, bot_id, "bot.roster.mutate")
    if not ok:
        return jsonify({"error": "forbidden", "detail": reason}), 403
    role = body.get("role")
    surfaces = body.get("engagement_surfaces")
    notes = body.get("notes")
    if role is None and surfaces is None and notes is None:
        return jsonify({"error": "no fields to update"}), 400
    if role == "blocked":
        return jsonify({
            "error": "use /block endpoint to set role=blocked"
        }), 400
    if role is not None and role not in ro.ROLES:
        return jsonify({"error": f"invalid role: {role!r}"}), 400

    by = _requester_label()
    shared = _shared_dir_from_net(net)

    def _mutate(overlay: dict) -> dict:
        if role is not None:
            ro.set_identity_role(overlay, channel, ext_id, role, by=by)
        if surfaces is not None:
            try:
                ro.set_identity_engagement(
                    overlay, channel, ext_id, surfaces, by=by)
            except ValueError as e:
                raise _PairingError(str(e)) from e
        if notes is not None:
            ids = overlay.setdefault("identities", {})
            key = ro.identity_key(channel, ext_id)
            entry = dict(ids.get(key) or {})
            entry["notes"] = str(notes)
            ids[key] = entry
        return ro.get_identity(overlay, channel, ext_id) or {}

    try:
        ro.mutate_overlay(shared, bot_id, "patch_identity", by, _mutate)
    except _PairingError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({
        "ok": True,
        "by_channel": _read_per_channel(net, bot_id),
        "blocked": _blocked_list(ro.load_overlay(shared, bot_id)),
    })


def _handle_overlay_block(network_path: Path, bot_id: str,
                          channel: str, ext_id: str) -> Any:
    """Block: overlay block index + revoke from OC allowFrom + reject pending."""
    body = request.get_json(silent=True) or {}
    if channel not in KNOWN_PROVIDERS:
        return jsonify({"error": "invalid 'channel'"}), 400
    net = load_network(network_path)
    if not _bot_exists(net, bot_id):
        return jsonify({"error": f"unknown bot: {bot_id}"}), 404
    ok, deny_reason = _check_capability(net, bot_id, "bot.roster.mutate")
    if not ok:
        return jsonify({"error": "forbidden", "detail": deny_reason}), 403
    reason = (body.get("reason") or "").strip()
    by = _requester_label()
    shared = _shared_dir_from_net(net)

    # Step 1: write the overlay block index. We do this BEFORE touching
    # OC's allowFrom so that even if the file write below fails (sudo
    # grant missing, etc.), the block is recorded and the next GET will
    # still resolve this identity as blocked. The reverse order would
    # leave a hole (revoked but not blocked → could be re-paired).
    def _mutate(overlay: dict) -> dict:
        return ro.block_identity(
            overlay, channel, ext_id, by=by, reason=reason)

    try:
        ro.mutate_overlay(shared, bot_id, "block", by, _mutate)
    except OSError as e:
        return jsonify({"error": f"overlay write failed: {e}"}), 500

    # Step 2: revoke from OC allowFrom. Idempotent — safe to call even
    # if the user wasn't there.
    try:
        _revoke(net, bot_id, channel, ext_id, None)
    except _PairingError as e:
        # The block is in place but allowFrom revoke failed. Surface the
        # partial state to the caller so they can retry. Block index
        # entry remains, which is the safer half-state.
        return jsonify({
            "error": f"allowFrom revoke failed (block recorded): {e}",
            "partial": True,
        }), 500

    # Step 3: drop any pending pairing for this id so they can't slide
    # in via the existing pairing-code flow before unblock.
    try:
        _reject(net, bot_id, channel, ext_id, None)
    except _PairingError:
        # Pending may not exist — _reject is idempotent at the file
        # level. A genuine write failure here is the same recovery
        # shape as Step 2; surface as partial but don't unwind the
        # block.
        pass

    return jsonify({
        "ok": True,
        "by_channel": _read_per_channel(net, bot_id),
        "blocked": _blocked_list(ro.load_overlay(shared, bot_id)),
    })


def _handle_overlay_unblock(network_path: Path, bot_id: str,
                            channel: str, ext_id: str) -> Any:
    """Remove from overlay block index. Does NOT re-add to allowFrom."""
    if channel not in KNOWN_PROVIDERS:
        return jsonify({"error": "invalid 'channel'"}), 400
    net = load_network(network_path)
    if not _bot_exists(net, bot_id):
        return jsonify({"error": f"unknown bot: {bot_id}"}), 404
    ok, deny_reason = _check_capability(net, bot_id, "bot.roster.mutate")
    if not ok:
        return jsonify({"error": "forbidden", "detail": deny_reason}), 403
    by = _requester_label()
    shared = _shared_dir_from_net(net)
    removed = {"present": False}

    def _mutate(overlay: dict) -> dict:
        removed["present"] = ro.unblock_identity(
            overlay, channel, ext_id, by=by)
        return {"removed": removed["present"]}

    try:
        ro.mutate_overlay(shared, bot_id, "unblock", by, _mutate)
    except OSError as e:
        return jsonify({"error": f"overlay write failed: {e}"}), 500
    return jsonify({
        "ok": True,
        "removed": removed["present"],
        "by_channel": _read_per_channel(net, bot_id),
        "blocked": _blocked_list(ro.load_overlay(shared, bot_id)),
    })


def _handle_overlay_ignore(network_path: Path, bot_id: str,
                           channel: str, ext_id: str) -> Any:
    """Add an identity to the overlay ignore index (triage dismiss).

    No allowFrom / pairing side effects — ignoring is purely a Users-page
    triage action that hides the seen-recently row. Gated by the same
    ``bot.roster.mutate`` capability as block/patch.
    """
    body = request.get_json(silent=True) or {}
    if channel not in KNOWN_PROVIDERS:
        return jsonify({"error": "invalid 'channel'"}), 400
    net = load_network(network_path)
    if not _bot_exists(net, bot_id):
        return jsonify({"error": f"unknown bot: {bot_id}"}), 404
    ok, deny_reason = _check_capability(net, bot_id, "bot.roster.mutate")
    if not ok:
        return jsonify({"error": "forbidden", "detail": deny_reason}), 403
    reason = (body.get("reason") or "").strip()
    by = _requester_label()
    shared = _shared_dir_from_net(net)

    def _mutate(overlay: dict) -> dict:
        return ro.ignore_identity(
            overlay, channel, ext_id, by=by, reason=reason)

    try:
        ro.mutate_overlay(shared, bot_id, "ignore", by, _mutate)
    except OSError as e:
        return jsonify({"error": f"overlay write failed: {e}"}), 500

    return jsonify({
        "ok": True,
        "by_channel": _read_per_channel(net, bot_id),
        "blocked": _blocked_list(ro.load_overlay(shared, bot_id)),
    })


def _handle_newcomer_mode_put(network_path: Path, bot_id: str,
                              channel: str) -> Any:
    """PUT per-channel newcomer mode + optional default engagement."""
    body = request.get_json(silent=True) or {}
    if channel not in KNOWN_PROVIDERS:
        return jsonify({"error": "invalid 'channel'"}), 400
    net = load_network(network_path)
    if not _bot_exists(net, bot_id):
        return jsonify({"error": f"unknown bot: {bot_id}"}), 404
    ok, deny_reason = _check_capability(net, bot_id, "bot.channel.config")
    if not ok:
        return jsonify({"error": "forbidden", "detail": deny_reason}), 403
    mode = body.get("mode")
    if mode not in ro.NEWCOMER_MODES:
        return jsonify({
            "error": f"invalid mode {mode!r}; "
                     f"expected one of {list(ro.NEWCOMER_MODES)}"
        }), 400
    surfaces = body.get("default_engagement_surfaces")
    by = _requester_label()
    shared = _shared_dir_from_net(net)

    def _mutate(overlay: dict) -> dict:
        try:
            return ro.set_channel_newcomer_mode(
                overlay, channel, mode, by=by,
                default_engagement_surfaces=surfaces)
        except ValueError as e:
            raise _PairingError(str(e)) from e

    try:
        ro.mutate_overlay(shared, bot_id, "set_newcomer_mode", by, _mutate)
    except _PairingError as e:
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        return jsonify({"error": f"overlay write failed: {e}"}), 500
    return jsonify({
        "ok": True,
        "by_channel": _read_per_channel(net, bot_id),
        "blocked": _blocked_list(ro.load_overlay(shared, bot_id)),
    })


def _auto_approve_inline(network: dict, bot_id: str) -> None:
    """Auto-approve pending requests on every GET to the Users page.

    Delegates to the shared ``pairing.auto_approver`` module which
    handles the three triggers (pod-admin, primary-owner, per-channel
    auto_admit) plus the overlay block-index check. Identical write
    path as before — ``_approve`` is the same function.

    The same sweep runs periodically via the
    ``ai.evolve.evolve.pairing-sweep`` LaunchDaemon so auto-admit
    fires without an admin on the page. Inline + periodic is the
    belt-and-suspenders shape; one absorbs visible operator activity,
    the other catches everything else.
    """
    # Local import — pairing.auto_approver imports back into this
    # module via the ``rbu_module`` injection to avoid a circular
    # import at module-load time.
    from ..pairing.auto_approver import run_one_pass as _sweep
    _sweep(network, bot_id, rbu_module=sys.modules[__name__])
