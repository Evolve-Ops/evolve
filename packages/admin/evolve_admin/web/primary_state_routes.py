"""primary_state_routes.py — `/api/primary/state/*` for the primary bot's
pod-state read tools.

Spec: internal/spec-primary-bot-interface-2026-05-14.md §5. These routes
are thin wrappers around ``evolve_admin.pod_state`` functions; the
real work lives there. One library implementation, two transports
(this HTTP layer + the MCP bridge per §5.2).

Routes:

  GET  /api/primary/state/pod_status                   → high-level pod summary
  GET  /api/primary/state/signals?state=&producer=&bot=&limit=  → filtered Signal list
  GET  /api/primary/state/proposals?state=&limit=      → Proposal summaries
  GET  /api/primary/state/watchdog?hours=&limit=&bot=  → WatchdogEvent tail
  GET  /api/primary/state/spend?window=&bot=           → spend rollup (1d/7d/30d)
  GET  /api/primary/state/turns?bot=&limit=            → recent USER turns
  GET  /api/primary/state/describe?bot=                → composed per-bot snapshot
  GET  /api/primary/state/audits?bot=&limit=           → latest OC audit results

Auth: loopback-only, same posture as /api/evo/* and /api/better/*. The
spec calls for primary-bot-token gating (§5.3); that's deferred to the
admin-auth pass (the existing /api/evo/intake endpoint also relies on
loopback today). All four routes are read-only and pure: an attacker
who reaches them can only learn pod state, not change it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, Response

from ..config import load_network
from ..pod_state import (
    describe_bot,
    list_audits,
    list_proposals,
    list_signals,
    pod_status,
    recent_turns,
    recent_watchdog,
    spend_rollup,
)


def _shared_dir_from_network(network: dict[str, Any]) -> Path:
    return Path(network.get("sharedDir", "/Users/Shared/evolve"))


def _validate_bot(bot: str, network: dict[str, Any]) -> tuple[str | None, str | None]:
    """Validate a bot id from query string against network.json.

    Returns ``(bot_id, None)`` on success or ``(None, error_msg)`` on
    failure. Caller is expected to short-circuit with 400 on failure.

    This is the load-bearing guard against path traversal for tools
    that build filesystem paths from the bot id (recent_turns reads
    ``{shared_dir}/metrics/{bot}/recent-transcripts.json``; spend_rollup
    reads ``{shared_dir}/metrics/{bot}/cost-*.json``). Without it,
    ``bot=../../../etc/passwd`` walked out of shared_dir at the
    library layer — caught in second-pass review of Bundle 3-part-2.

    The validation is whitelist-based: the id must appear in
    ``network.bots``. This is stricter than a regex (which would still
    let a real-looking id like ``other_pod_bot`` through if it ever
    existed under metrics/) and matches the principle that
    network.json is the source of truth for pod membership
    (``feedback_explicit_pod_membership.md`` memory).
    """
    cleaned = (bot or "").strip()
    if not cleaned:
        return None, "bot is required"
    bots_block = network.get("bots") or {}
    if cleaned not in bots_block:
        return None, f"unknown bot: {cleaned}"
    return cleaned, None


def register_primary_state_routes(app: Flask, network_path: Path) -> None:
    """Register all /api/primary/state/* routes on the Flask app."""

    @app.get("/api/primary/state/pod_status")
    def api_primary_pod_status() -> Response:
        """High-level pod summary — bots, primary id, firing-signal counts.

        Pure read; no parameters. Cheap enough that the bot can call it
        opportunistically when admin opens a session.
        """
        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        return jsonify(pod_status(shared_dir, network=network))

    @app.get("/api/primary/state/signals")
    def api_primary_signals() -> Response:
        """Recent Signals, filtered by query string.

        Query params:
          state    optional — firing|snoozed|resolved|dismissed|all (default firing)
          producer optional — generator/monitor id (e.g. cost_alert)
          bot      optional — bot id
          limit    optional — N (default 20, max 100)
        """
        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        state_arg = request.args.get("state") or "firing"
        producer = request.args.get("producer") or None
        bot = request.args.get("bot") or None
        try:
            limit = int(request.args.get("limit", "20"))
        except (TypeError, ValueError):
            limit = 20
        return jsonify(list_signals(
            shared_dir,
            state=state_arg,
            producer=producer,
            bot_id=bot,
            limit=limit,
        ))

    @app.get("/api/primary/state/proposals")
    def api_primary_proposals() -> Response:
        """Proposal summaries, filtered by state.

        Query params:
          state  optional — pending|snoozed|applied|archived|active|all (default pending)
          limit  optional — N (default 20, max 100)
        """
        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        state_arg = request.args.get("state") or "pending"
        try:
            limit = int(request.args.get("limit", "20"))
        except (TypeError, ValueError):
            limit = 20
        return jsonify(list_proposals(
            shared_dir,
            state=state_arg,
            limit=limit,
        ))

    @app.get("/api/primary/state/watchdog")
    def api_primary_watchdog() -> Response:
        """Watchdog event tail.

        Query params:
          hours  optional — lookback window (default 24, max 168 = one week)
          limit  optional — N (default 20, max 100)
          bot    optional — bot id
        """
        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        try:
            hours = int(request.args.get("hours", "24"))
        except (TypeError, ValueError):
            hours = 24
        try:
            limit = int(request.args.get("limit", "20"))
        except (TypeError, ValueError):
            limit = 20
        bot = request.args.get("bot") or None
        return jsonify(recent_watchdog(
            shared_dir,
            hours=hours,
            limit=limit,
            bot_id=bot,
        ))

    @app.get("/api/primary/state/spend")
    def api_primary_spend() -> Response:
        """Cost / token rollup over a 1d/7d/30d window.

        Query params:
          window  optional — 1d|7d|30d (default 7d)
          bot     optional — bot id; omit for pod-wide totals
        """
        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        window = request.args.get("window") or "7d"
        bot_raw = request.args.get("bot") or None
        if bot_raw is not None:
            bot, err = _validate_bot(bot_raw, network)
            if err:
                return jsonify({"error": err}), 400
        else:
            bot = None
        return jsonify(spend_rollup(
            shared_dir,
            window=window,
            bot_id=bot,
            network=network,
        ))

    @app.get("/api/primary/state/turns")
    def api_primary_turns() -> Response:
        """Recent USER turns recorded for the named bot.

        Spec §5.1: subject to security_warden_capture_policy (USER text
        only, 200/48h cap, per-bot opt-out via
        network.bots[id].securityScanning).

        Query params:
          bot    required — bot id whose buffer to read
          limit  optional — N (default 10, max 50)
        """
        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        bot, err = _validate_bot(request.args.get("bot", ""), network)
        if err:
            return jsonify({"error": err}), 400
        try:
            limit = int(request.args.get("limit", "10"))
        except (TypeError, ValueError):
            limit = 10
        return jsonify(recent_turns(
            shared_dir,
            bot_id=bot,
            limit=limit,
        ))

    @app.get("/api/primary/state/describe")
    def api_primary_describe() -> Response:
        """Composed snapshot of one bot.

        Query params:
          bot  required — bot id to describe
        """
        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        bot, err = _validate_bot(request.args.get("bot", ""), network)
        if err:
            return jsonify({"error": err}), 400
        return jsonify(describe_bot(
            shared_dir,
            bot_id=bot,
            network=network,
        ))

    @app.get("/api/primary/state/audits")
    def api_primary_audits() -> Response:
        """Latest OC security audit result(s).

        Query params:
          bot    optional — filter to one bot (must be in network.bots)
          limit  optional — cap on number of bots returned (default 5, max 50)

        Returns ``stale=true`` when no audit sweep has populated the
        cache since the admin server started or the cache is older
        than the freshness window — the bot can render "audit data
        is stale, admin should refresh."
        """
        network = load_network(network_path)
        shared_dir = _shared_dir_from_network(network)
        bot_raw = request.args.get("bot") or None
        if bot_raw is not None:
            bot, err = _validate_bot(bot_raw, network)
            if err:
                return jsonify({"error": err}), 400
        else:
            bot = None
        try:
            limit = int(request.args.get("limit", "5"))
        except (TypeError, ValueError):
            limit = 5
        return jsonify(list_audits(
            shared_dir,
            bot_id=bot,
            limit=limit,
        ))
