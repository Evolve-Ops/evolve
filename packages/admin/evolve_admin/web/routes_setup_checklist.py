"""Admin per-bot Setup checklist routes.

Backs the "Setup checklist" card on the per-bot Settings page and the
``Setup X/N`` chip on the Overview tile (step 4 of the rollout — chip+modal
wires through the same endpoints).

Endpoints (all under ``/api/admin/bots/<bot_id>/setup-checklist``)::

  GET  /                          → full render payload (see _payload)
  POST /items/<item_id>           → body ``{"state": "dismissed"|"pending"}``
  POST /suppress                  → hide the tile chip
  POST /reset                     → bring the tile chip back

The GET runs the auto-detectors, persists any pending↔done transitions
to network.json, and returns the merged render payload. POST endpoints
mutate state and return the same payload shape so the UI can re-render
without a second round-trip.

Spec discussion 2026-06-01. Data layer: ``evolve_admin.setup_checklist``.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request

from .. import setup_checklist as sc
from ..config import (
    bot_home,
    ensure_reserved_bot_purposes,
    is_reserved_account,
    load_network,
    save_network,
)

log = logging.getLogger(__name__)


def _seed_reserved_purpose(network: dict, bot_id: str) -> bool:
    """Self-heal a built-in assistant's (evo/evolve) default purpose so its
    Setup-checklist purpose row reads Done without waiting for a fresh deploy.

    No-op for member bots, and for a reserved bot that already has a purpose
    (operator edits are never overwritten — see
    ``config.ensure_reserved_bot_purposes``). Returns True iff it mutated
    ``network``; the caller folds that into its ``dirty`` flag so the seed is
    persisted. Must run BEFORE ``evaluate`` so the purpose detector reads the
    seeded value in the same response.

    This is the live-pod seam: deploy.py (the natural deploy-time home) is a
    frozen hot-hazard file under a no-growth cap, so the backfill rides the
    admin status/checklist read path the running evo's dashboard already hits.
    Fresh pods seed earlier, in the setup wizard's network build.
    """
    if not is_reserved_account(bot_id, network):
        return False
    return ensure_reserved_bot_purposes(network)


def register_routes(app: Flask, network_path: Path) -> None:
    """Register per-bot Setup-checklist endpoints on ``app``."""

    @app.get("/api/admin/bots/<bot_id>/setup-checklist")
    def setup_checklist_get(bot_id: str) -> Any:  # type: ignore[no-redef]
        net = load_network(network_path)
        if not _bot_exists(net, bot_id):
            return jsonify({"error": f"unknown bot: {bot_id}"}), 404
        payload, dirty = _evaluate_and_payload(net, bot_id, network_path)
        if dirty:
            _persist(net, network_path)
        return jsonify(payload)

    @app.post("/api/admin/bots/<bot_id>/setup-checklist/items/<item_id>")
    def setup_checklist_set_item(bot_id: str, item_id: str) -> Any:  # type: ignore[no-redef]
        body = request.get_json(silent=True) or {}
        state = (body.get("state") or "").strip()
        # Clients can only flip an item to dismissed (operator said "no
        # thanks") or pending (un-dismiss so the detector takes over again).
        # ``done`` is auto-detector territory — accepting it from the UI
        # would let operators bypass the actual check.
        if state not in (sc.STATE_DISMISSED, sc.STATE_PENDING):
            return jsonify({"error": f"state must be 'dismissed' or 'pending', got {state!r}"}), 400
        net = load_network(network_path)
        if not _bot_exists(net, bot_id):
            return jsonify({"error": f"unknown bot: {bot_id}"}), 404
        if item_id not in sc.ITEMS_BY_ID:
            return jsonify({"error": f"unknown checklist item: {item_id}"}), 404
        sc.set_item_state(net, bot_id, item_id, state)
        # Re-evaluate so the GET-shape payload reflects the updated state
        # plus any auto-detector flips. evaluate() preserves dismissed.
        payload, _ = _evaluate_and_payload(net, bot_id, network_path)
        _persist(net, network_path)
        return jsonify(payload)

    @app.post("/api/admin/bots/<bot_id>/setup-checklist/suppress")
    def setup_checklist_suppress(bot_id: str) -> Any:  # type: ignore[no-redef]
        net = load_network(network_path)
        if not _bot_exists(net, bot_id):
            return jsonify({"error": f"unknown bot: {bot_id}"}), 404
        sc.suppress_chip(net, bot_id)
        payload, _ = _evaluate_and_payload(net, bot_id, network_path)
        _persist(net, network_path)
        return jsonify(payload)

    @app.post("/api/admin/bots/<bot_id>/setup-checklist/reset")
    def setup_checklist_reset(bot_id: str) -> Any:  # type: ignore[no-redef]
        net = load_network(network_path)
        if not _bot_exists(net, bot_id):
            return jsonify({"error": f"unknown bot: {bot_id}"}), 404
        sc.reset_chip(net, bot_id)
        payload, _ = _evaluate_and_payload(net, bot_id, network_path)
        _persist(net, network_path)
        return jsonify(payload)


# ── Helpers ───────────────────────────────────────────────────────────────


def _bot_exists(network: dict, bot_id: str) -> bool:
    return bot_id in (network.get("bots") or {})


def _load_openclaw_cfg(bot_id: str, network: dict) -> "dict | None":
    """Read a bot's openclaw.json. Direct read → sudo /bin/cat fallback → None.

    Mirrors the resilient-read pattern in admin_bot_routes._load_openclaw_for_bot
    and web.routes_bot_users._read_json_or_none. Kept local so this module
    doesn't import a Flask-attached neighbor just for one helper.
    """
    try:
        oc_path = bot_home(bot_id, network) / ".openclaw" / "openclaw.json"
    except Exception:  # noqa: BLE001
        return None
    text: "str | None" = None
    try:
        text = oc_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (PermissionError, OSError):
        text = None
    if text is None:
        try:
            r = subprocess.run(
                ["sudo", "-n", "/bin/cat", str(oc_path)],
                check=True, capture_output=True, text=True, timeout=5,
            )
            text = r.stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _evaluate_and_payload(
    network: dict, bot_id: str, network_path: Path,
) -> tuple[dict, bool]:
    """Run detectors and build the render payload. Second return is the
    dirty flag from ``sc.evaluate_dirty`` — caller persists only when
    state actually changed (avoids the ``sudo chown`` on every read).
    """
    openclaw_cfg = _load_openclaw_cfg(bot_id, network)
    # Seed a built-in assistant's purpose before evaluating, so its purpose row
    # reads Done in this same response (no-op for member bots / already-set).
    seeded = _seed_reserved_purpose(network, bot_id)
    merged, dirty = sc.evaluate_dirty(network, bot_id, openclaw_cfg)

    done, total = sc.counter(merged)
    chip_visible = sc.is_chip_visible(network, bot_id, merged)
    in_menu = sc.should_show_in_actions_menu(network, bot_id, merged)
    suppressed_at = sc.get_state(network, bot_id).get("tile_chip_suppressed_at")

    # Preserve registry order in the response so the UI can render the
    # list directly without resorting. Dicts in 3.7+ keep insertion order
    # but the JSON spec is undefined on order, so build an explicit list.
    items = [
        {"id": item.id, **merged[item.id]}
        for item in sc.CHECKLIST_ITEMS
        if item.id in merged
    ]
    return (
        {
            "bot_id": bot_id,
            "items": items,
            "counter": {"done": done, "total": total},
            "tile_chip": {
                "visible": chip_visible,
                "in_actions_menu": in_menu,
                "suppressed_at": suppressed_at,
            },
        },
        dirty or seeded,
    )


def _persist(network: dict, network_path: Path) -> None:
    """save_network with a swallowed failure log — never break the request
    on a persistence error (the caller already got a logical-correct
    response; the next read will re-detect)."""
    try:
        save_network(network, network_path)
    except Exception as e:  # noqa: BLE001
        log.warning("save_network failed for setup-checklist write: %s", e)


def enrich_tiles_with_setup_chip(
    network: dict, bots_data: dict,
) -> bool:
    """Attach the ``setup_progress`` chip + ``setup_in_actions_menu`` flag
    to each bot's tile dict in ``bots_data``.

    Called from ``/api/status`` after ``compute_tile_data`` populates
    ``bot_data["tile"]``. Mutates ``bots_data`` in place. Returns True
    iff at least one bot's persisted setup_checklist state was mutated
    by the evaluate call — the caller uses this flag to decide whether
    to ``save_network``, avoiding a sudo chown on every quiet refresh.

    Pulled out of server.py's inline block so the enrichment is
    unit-testable without spinning up the full Flask app.
    """
    any_dirty = False
    for bot_id, bot_data in (bots_data or {}).items():
        try:
            # Built-in assistants backfill their default purpose on the Overview
            # status poll — the live-pod self-heal seam (deploy.py is capped).
            if _seed_reserved_purpose(network, bot_id):
                any_dirty = True
            oc_cfg = _load_openclaw_cfg(bot_id, network)
            merged, dirty = sc.evaluate_dirty(network, bot_id, oc_cfg)
            if dirty:
                any_dirty = True
            tile = bot_data.setdefault("tile", {})
            chips = tile.setdefault("health_chips", [])
            if sc.is_chip_visible(network, bot_id, merged):
                done, total = sc.counter(merged)
                chips.append({
                    "id": "setup_progress",
                    "severity": "info",
                    "label": f"Setup {done}/{total}",
                    "detail": f"{total - done} item(s) left to set up",
                    # Frontend dispatches on this — opens the
                    # setup-checklist modal for this bot instead of
                    # the default chipNav page navigation.
                    "click_action": "modal:setup_checklist",
                })
            tile["setup_in_actions_menu"] = sc.should_show_in_actions_menu(
                network, bot_id, merged,
            )
        except Exception:
            # Never let one bot's checklist failure tank the whole
            # tile-enrichment pass. The chip will re-attempt on the
            # next refresh.
            continue
    return any_dirty
