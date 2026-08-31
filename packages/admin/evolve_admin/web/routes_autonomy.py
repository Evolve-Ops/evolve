"""HTTP routes for the autonomy ladder (per-bot, per-integration posture).

/api/autonomy/*    Security → Permissions → Autonomy section, per
                   internal/spec-autonomy-ladder-2026-06-10.md §3.1 + §4.1.

One API for both directions (promotion and demotion) — the friction
asymmetry (confirmation up, one click down) lives in the UI, not here.
Phase A applies posture changes directly through ``autonomy.store`` +
``autonomy.renderer``; the proposal-pipeline applier
(``UpdateAutonomyPosture``) is Phase B and serves the
improvement-pipeline path, not this operator-direct one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, Response

from evolve_config import CANONICAL_SHARED_DIR

from ..config import load_network
from ..telemetry import get_logger

_log = get_logger("web.routes_autonomy")


def _register_autonomy_routes(app: Flask, network_path: Path) -> None:
    """Register /api/autonomy/* endpoints."""

    def _all_bots() -> list[str]:
        return list(load_network(network_path).get("bots", {}).keys())

    def _shared_dir() -> Path:
        return Path(load_network(network_path).get("sharedDir", str(CANONICAL_SHARED_DIR)))

    def _rows_for_bot(bot_id: str, shared: Path) -> list[dict[str, Any]]:
        """Build the UI rows for one bot's ladder-eligible integrations.

        Calls ``ensure_backfilled`` first (idempotent find-or-create,
        spec §5.2) so a pod that predates the ladder shows rows on
        first page load without waiting for the monitor sweep. Bots
        with no ladder-eligible integrations return ``[]`` and the UI
        renders nothing for them (no dead affordances, spec §4.1).
        """
        from autonomy import backfill as _ab
        from autonomy import catalog as _acat
        from autonomy import coherence as _ac
        from autonomy import store as _astore

        try:
            # collect_findings=False: the route only needs the entries to
            # exist; the suggestion findings are the monitor's to emit.
            _ab.ensure_backfilled(shared, bot_id, collect_findings=False)
        except Exception as exc:  # noqa: BLE001 — rows still render from the file
            _log.warning("autonomy backfill failed for %s: %s", bot_id, exc)

        try:
            doc = _astore.load(shared, bot_id)
        except ValueError as exc:
            return [{"error": f"posture file invalid: {exc}", "bot_id": bot_id}]
        if doc is None or not doc.integrations:
            return []

        # Live coherence per integration (expected render vs live state),
        # so the enforcement badge is honest even between monitor sweeps.
        drift_by_iid: dict[str, dict[str, Any]] = {}
        try:
            for f in _ac.check_bot(bot_id, shared):
                iid = (f.get("details") or {}).get("integration_id")
                if iid:
                    drift_by_iid[iid] = f["details"]
        except Exception as exc:  # noqa: BLE001
            _log.warning("autonomy coherence failed for %s: %s", bot_id, exc)

        rows: list[dict[str, Any]] = []
        for iid in sorted(doc.integrations.keys()):
            posture = doc.integrations[iid]
            spec = _acat.kind_spec(posture.kind)
            binding = _acat.binding_for(iid)
            if spec is None or binding is None:
                continue
            up = _acat.next_rung_up(posture.rung)
            down = _acat.next_rung_down(posture.rung)
            unconfirmed = (
                (posture.set_by or {}).get("actor") == _astore.ACTOR_BACKFILL
            )
            drift = drift_by_iid.get(iid)
            rows.append({
                "integration_id": iid,
                "kind": posture.kind,
                "display_name": binding.display_name,
                "integration_label": f"{spec.operator_noun} ({binding.display_name})",
                "rung": posture.rung,
                "rung_label": _acat.RUNG_LABELS.get(posture.rung, posture.rung),
                "rung_meaning": spec.rung_meanings.get(posture.rung, ""),
                "rules": posture.rules,
                "set_by": posture.set_by,
                "set_at": posture.set_at,
                "history": posture.history,
                "enforcement": posture.enforcement,
                # §2.4 honesty: mechanical → "enforced"; procedural →
                # "instructed and monitored". Unrendered (observe-only
                # backfill) and drifted states are their own labels.
                "enforcement_mode": spec.rung_enforcement_mode.get(posture.rung, "procedural"),
                "unconfirmed": unconfirmed,
                "in_sync": drift is None,
                "default_rung": spec.default_rung,
                "promote": None if up is None else {
                    "rung": up,
                    "rung_label": _acat.RUNG_LABELS.get(up, up),
                    "consequence": spec.promotion_consequences.get(up, ""),
                    "requires_rules": up == _acat.RUNG_AUTONOMOUS,
                },
                "demote": None if down is None else {
                    "rung": down,
                    "rung_label": _acat.RUNG_LABELS.get(down, down),
                },
            })
        return rows

    @app.get("/api/autonomy/inventory")
    def api_autonomy_inventory() -> "Response | tuple[Response, int]":
        """Posture rows for every bot. Bots with no ladder-eligible
        integrations are omitted entirely."""
        try:
            # Import OUTSIDE the per-bot loop: a missing/broken autonomy
            # package must surface as a 500, not as every bot silently
            # losing its rows (an empty section is indistinguishable from
            # "nothing is ladder-eligible" by design).
            import autonomy.backfill  # noqa: F401
            import autonomy.catalog  # noqa: F401
            import autonomy.coherence  # noqa: F401
            import autonomy.store  # noqa: F401

            shared = _shared_dir()
            bots: dict[str, Any] = {}
            for bot_id in _all_bots():
                try:
                    rows = _rows_for_bot(bot_id, shared)
                except Exception as exc:  # noqa: BLE001 — per-bot isolation
                    _log.warning("autonomy rows failed for %s: %s", bot_id, exc)
                    # Honest failure: an error row, never a vanished section.
                    rows = [{
                        "error": f"autonomy settings unavailable: {exc}",
                        "bot_id": bot_id,
                    }]
                if rows:
                    bots[bot_id] = {"integrations": rows}
            return jsonify({"bots": bots})
        except ImportError as exc:
            return jsonify({"ok": False, "error": f"autonomy module not importable: {exc}"}), 500

    @app.post("/api/autonomy/<bot_id>/<integration_id>")
    def api_autonomy_set(bot_id: str, integration_id: str) -> "Response | tuple[Response, int]":
        """Set an integration's autonomy level (promotion AND demotion).

        Body: {rung: str, rules?: {}, expected_current_rung?: str|null,
               actor?: "operator_ui" | "primary_bot"}

        ``expected_current_rung`` is the rung the UI displayed when the
        operator clicked — the compare-and-swap guard. A mismatch (a
        concurrent change landed first) returns 409 and the UI reloads.

        ``actor`` defaults to operator_ui; evo's ``action.autonomy.set``
        tool passes primary_bot so the history shows the chat front door
        (spec §3.1 — same API, same validation, same history record).
        Only those two values are accepted: provenance actors
        (proposal:* / auto_demotion:*) are written by their own code
        paths, never via HTTP.
        """
        from autonomy import renderer as _arend
        from autonomy import store as _astore

        if bot_id not in _all_bots():
            return jsonify({"ok": False, "error": f"unknown bot {bot_id!r}"}), 404

        data = request.get_json(silent=True) or {}
        rung = (data.get("rung") or "").strip()
        rules = data.get("rules") or {}
        if not rung:
            return jsonify({"ok": False, "error": "rung required"}), 400
        if not isinstance(rules, dict):
            return jsonify({"ok": False, "error": "rules must be an object"}), 400
        actor = str(data.get("actor") or _astore.ACTOR_OPERATOR_UI)
        if actor not in (_astore.ACTOR_OPERATOR_UI, _astore.ACTOR_PRIMARY_BOT):
            return jsonify({"ok": False, "error": f"unknown actor {actor!r}"}), 400

        shared = _shared_dir()
        kwargs: dict[str, Any] = {}
        if "expected_current_rung" in data:
            kwargs["expected_current_rung"] = data.get("expected_current_rung")
        try:
            posture = _astore.set_posture(
                shared, bot_id, integration_id,
                rung=rung, rules=rules,
                actor=actor,
                note=str(data.get("note") or ""),
                **kwargs,
            )
        except _astore.StalePostureError as exc:
            return jsonify({"ok": False, "error": str(exc), "stale": True}), 409
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        # Render immediately — the posture write IS the render trigger
        # (spec §2.3). render_bot never raises; failures land in
        # write_error and surface to the operator + the drift check.
        render = _arend.render_bot(bot_id, shared)
        body: dict[str, Any] = {
            "ok": True,
            "bot_id": bot_id,
            "integration_id": integration_id,
            "rung": posture.rung,
            "render": {
                "changed": render.changed,
                "written": render.written,
                "error": render.write_error,
            },
        }
        if render.write_error:
            body["warning"] = (
                "Setting recorded, but applying it to the bot failed: "
                f"{render.write_error}. The next deploy re-applies it; "
                "the drift check will flag it until then."
            )
        return jsonify(body)
