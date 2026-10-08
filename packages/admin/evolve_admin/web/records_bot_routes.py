"""Bot-facing records verbs — ``/api/records-bot/<app_id>/…`` (D-AD2, D-AD3).

Design: ``internal/design-app-records-layer-2026-09-26.md`` §2.1/§2.3. Brief:
``app-store-and-ledger-verbs``. Store: ``app_store`` / ``app_ledger``.

    POST /api/records-bot/<app_id>/list    {table, filter?, sort?, limit?, instance?}  records.list
    POST /api/records-bot/<app_id>/get     {table, key, instance?}                     records.get
    POST /api/records-bot/<app_id>/put     {table, row, instance?}                     records.put
    POST /api/records-bot/<app_id>/delete  {table, key, instance?}                     records.delete
    POST /api/records-bot/<app_id>/history {table, key, instance?}                     records.history
    POST /api/records-bot/<app_id>/ledger/append {entry, instance?}                    ledger.append

IDENTITY (the board's model, ``board_bot_routes``): the calling bot is bound
from the unix-socket peer uid, never from the request. So the verb call names
its bot by construction, and ``app_store.authorize`` checks that bot's
binding to the app instance BEFORE the store file is opened. A TCP request or
an unmapped uid gets 403.

TWO DOORS BEFORE THE STORE (D-AD8). A platform-owned app id (``evolve.directory``
first) is refused for every verb, reads included: the directory's rows carry
audit and provenance the bot view withholds, and its writes have their own
audited verbs. And ``app_id`` is DECLARED, not discovered: the calling bot's
manifests must name the app, so a model-supplied string never selects a store.
Each refusal raises a Signal.

FAIL CLOSED: every refusal is the store's typed :class:`RecordsRefusal`,
returned as ``{error, code}`` with its status. Nothing here writes on its own
account — no fallback, no retry, no partial write.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from flask import Flask, jsonify
from flask.typing import ResponseReturnValue

from .. import app_contract, app_ledger, app_store
from ..app_store import RecordsRefusal
from ..config import CANONICAL_SHARED_DIR, load_network
from . import peer_auth
from .board_limits import read_bounded_json

log = logging.getLogger(__name__)

#: A put carries one row; 64 KiB is the store's JSON-column cap plus framing.
MAX_RECORDS_BODY = 96 * 1024

PREFIX = "/api/records-bot/"

PRODUCER = "records_bot"
TYPE_REFUSED_APP = "records_app_refused"


def bot_declared_app_ids(bot_id: str, shared_dir: Path) -> set[str]:
    """App ids the bot's own manifests declare. Raw manifests, resolved the way
    every other reader resolves identity; an unreadable file declares nothing."""
    from ..applications.app_identity import resolve_app_id
    from ..applications.manifest import applications_dir

    declared: set[str] = set()
    try:
        files = sorted(applications_dir(shared_dir, bot_id).glob("*.json"))
    except OSError:
        return declared
    for f in files:
        if f.name.startswith((".", "_")):
            continue
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            ids = {resolve_app_id(data)}
            prov = data.get("provenance")
            if isinstance(prov, dict):  # v7-arc instance: the spec id is the app
                # identity: see resolve_app_id — a v7-arc instance declares the app by its spec id; both names admit the store.
                ids.add(str(prov.get("spec_id") or "").strip())
            declared |= {i for i in ids if i}
    return declared


def _signal_refusal(shared_dir: Path, bot_id: str, app_id: str, why: str) -> None:
    """Best-effort: a refusal must never turn into a 500 for want of a Signal."""
    try:
        from schema.signal import make_signature
        from signals import store as signals_store

        signals_store.observe(
            shared_dir,
            signature=make_signature(PRODUCER, TYPE_REFUSED_APP, f"{bot_id}:{app_id}"),
            producer=PRODUCER, type=TYPE_REFUSED_APP, scope="bot", bot_id=bot_id,
            title=f"{bot_id} reached for app records it may not use ({app_id})",
            body=f"The records tool was called on {app_id!r}: {why}. Nothing was read or written.",
            details={"app_id": app_id, "reason": why},
        )
    except Exception:  # noqa: BLE001
        log.warning("records refusal signal not written for %s/%s", bot_id, app_id,
                    exc_info=True)


def register_records_bot_routes(app: Flask, network_path: Path) -> None:
    """Register the six bot-facing records verbs on ``app``."""

    def _shared_dir() -> Path:
        return Path(load_network(network_path).get("sharedDir", CANONICAL_SHARED_DIR))

    def _run(app_id: str, fn: Callable[[str, dict[str, Any]], dict[str, Any]]) -> ResponseReturnValue:
        bot_id = peer_auth.resolve_peer_bot_id(network_path)
        if bot_id is None:
            return jsonify({"error": "this endpoint is reachable only by a bot over "
                                     "the admin-daemon unix socket",
                            "code": "forbidden"}), 403
        why = None
        if app_store.is_platform_app(app_id):
            why = "it is a platform-owned store, reachable only through its own audited verbs"
        elif app_id not in bot_declared_app_ids(bot_id, _shared_dir()):
            why = f"bot {bot_id!r} does not declare that app"
        if why:
            _signal_refusal(_shared_dir(), bot_id, app_id, why)
            return jsonify({"error": f"app {app_id!r} is not available to the records tool: {why}.",
                            "code": "forbidden"}), 403
        body = read_bounded_json(MAX_RECORDS_BODY)
        if body is None:
            return jsonify({"error": "JSON object body required", "code": "bad_request"}), 400
        try:
            return jsonify(fn(bot_id, body))
        except RecordsRefusal as exc:
            if exc.code in ("store_failed", "busy"):
                log.warning("records verb refused for %s: %s", bot_id, exc)
            return jsonify(exc.to_dict()), exc.status

    def _inst(body: dict[str, Any]) -> Any:
        return body.get("instance") or None

    @app.post(PREFIX + "<app_id>/list")
    def records_bot_list(app_id: str) -> ResponseReturnValue:
        return _run(app_id, lambda bot, b: app_store.records_list(
            _shared_dir(), app_id, b.get("table"), caller=bot, instance=_inst(b),
            filter=b.get("filter"), sort=b.get("sort"), limit=b.get("limit")))

    @app.post(PREFIX + "<app_id>/get")
    def records_bot_get(app_id: str) -> ResponseReturnValue:
        return _run(app_id, lambda bot, b: app_store.records_get(
            _shared_dir(), app_id, b.get("table"), b.get("key"), caller=bot,
            instance=_inst(b)))

    @app.post(PREFIX + "<app_id>/put")
    def records_bot_put(app_id: str) -> ResponseReturnValue:
        return _run(app_id, lambda bot, b: app_store.records_put(
            _shared_dir(), app_id, b.get("table"), b.get("row"), caller=bot,
            instance=_inst(b)))

    @app.post(PREFIX + "<app_id>/delete")
    def records_bot_delete(app_id: str) -> ResponseReturnValue:
        return _run(app_id, lambda bot, b: app_store.records_delete(
            _shared_dir(), app_id, b.get("table"), b.get("key"), caller=bot,
            instance=_inst(b)))

    @app.post(PREFIX + "<app_id>/history")
    def records_bot_history(app_id: str) -> ResponseReturnValue:
        return _run(app_id, lambda bot, b: app_store.records_history(
            _shared_dir(), app_id, b.get("table"), b.get("key"), caller=bot,
            instance=_inst(b)))

    @app.post(PREFIX + "<app_id>/ledger/append")
    def records_bot_ledger_append(app_id: str) -> ResponseReturnValue:
        return _run(app_id, lambda bot, b: app_ledger.ledger_append(
            _shared_dir(), app_id, b.get("entry"), caller=bot, instance=_inst(b)))

    app_contract.require_route_rows(app, PREFIX)
