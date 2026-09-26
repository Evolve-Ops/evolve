"""HTTP routes for the MCP-admin surface (per-bot MCP-server inventory + allowlist).

/api/mcp-admin/*   Integrations → MCP Servers sub-tab and Security → MCP Posture
                   matrix per spec-mcp-administration-2026-05-10.md §7.

Distinct from /api/mcp/* (the Evolve→Claude-Desktop bridge).

Extracted from server.py (Batch E, Step 4).
"""

from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, request, Response

from ..config import load_network
from ..telemetry import get_logger

_log = get_logger("web.routes_mcp_admin")


def _register_mcp_admin_routes(app: Flask, network_path: Path) -> None:
    """Register /api/mcp-admin/* endpoints — per-bot MCP-server inventory + allowlist.

    Distinct from /api/mcp/* (the Evolve→Claude-Desktop bridge). These routes
    serve the Integrations → MCP Servers sub-tab and Security → MCP Posture
    matrix per spec-mcp-administration-2026-05-10.md §7.

    Phase A is read-only: inventory and allowlist views, plus an on-demand
    rescan trigger. Phase B will add install / remove / update endpoints
    that wrap the proposal pipeline.
    """
    def _all_bots() -> list[str]:
        return list(load_network(network_path).get("bots", {}).keys())

    def _shared_dir() -> Path:
        return Path(load_network(network_path).get("sharedDir", "/Users/Shared/evolve"))

    @app.get("/api/mcp-admin/inventory")
    def api_mcp_admin_inventory() -> Response:
        """Return cached per-bot MCP inventories + the pod allowlist.

        Reads {shared_dir}/mcp/inventory/<bot>.json (written by the
        audit-driven monitor). Each server entry is enriched with the
        most recent probe result and the count of cached advisories
        for the server's catalog package, so the UI can render health
        + CVE badges without extra round-trips.
        """
        try:
            from mcp_admin import inventory as _inv, allowlist as _al, probe as _probe
            from mcp_admin.catalog import load as _cat_load
            from mcp_admin import advisories as _adv
        except ImportError:
            return jsonify({"ok": False, "error": "mcp module not importable"}), 500
        shared = _shared_dir()
        bots = _all_bots()
        cat = _cat_load(shared)
        # Pre-build server_id → package_name + advisory count lookup
        pkg_by_server: dict[str, str] = {e.id: e.package_name for e in cat.entries if e.package_name}
        adv_count_by_pkg: dict[str, int] = {}
        for pkg in set(pkg_by_server.values()):
            adv_count_by_pkg[pkg] = len(_adv.load_advisories(shared, pkg))

        result: dict = {"bots": {}, "allowlist": _al.load(shared).to_dict()}
        for bid in bots:
            cached = _inv.load_inventory(shared, bid)
            if cached:
                for s in cached.get("servers", []) or []:
                    sname = s.get("name", "")
                    recent = _probe.read_recent(shared, bid, sname, limit=1)
                    s["last_probe"] = recent[0] if recent else None
                    pkg = pkg_by_server.get(sname)
                    s["advisory_count"] = adv_count_by_pkg.get(pkg, 0) if pkg else 0
                    s["package_name"] = pkg or ""
            result["bots"][bid] = cached
        return jsonify(result)

    @app.get("/api/mcp-admin/health/<bot_id>/<server_id>")
    def api_mcp_admin_health(bot_id: str, server_id: str) -> Response:
        """Return the recent probe history (newest-first) for one (bot, server)."""
        try:
            from mcp_admin import probe as _probe
        except ImportError:
            return jsonify({"ok": False, "error": "mcp module not importable"}), 500
        shared = _shared_dir()
        recent = _probe.read_recent(shared, bot_id, server_id, limit=50)
        return jsonify({"bot_id": bot_id, "server_id": server_id, "entries": recent})

    @app.get("/api/mcp-admin/advisories")
    def api_mcp_admin_advisories() -> Response:
        """Return all cached advisories keyed by package name (Phase D)."""
        try:
            from mcp_admin import advisories as _adv
        except ImportError:
            return jsonify({"ok": False, "error": "mcp module not importable"}), 500
        shared = _shared_dir()
        return jsonify({"packages": _adv.load_all_advisories(shared)})

    @app.post("/api/mcp-admin/advisories/refresh")
    def api_mcp_admin_advisories_refresh() -> Response:
        """Force-refresh the advisory cache for every catalog package.

        Bypasses the 24h staleness window. Used by the UI "↻ Refresh advisories"
        button and worth running after editing the catalog.
        """
        try:
            from mcp_admin import advisories as _adv, catalog as _cat
        except ImportError:
            return jsonify({"ok": False, "error": "mcp module not importable"}), 500
        shared = _shared_dir()
        try:
            results = _adv.refresh_catalog(shared, catalog=_cat.load(shared), force=True)
        except Exception as e:
            return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500
        refreshed = sum(1 for r in results if r.get("refreshed"))
        return jsonify({"ok": True, "results": results, "refreshed": refreshed})

    @app.post("/api/mcp-admin/scan")
    def api_mcp_admin_scan() -> Response:
        """Run the MCP monitor on demand (rather than waiting for next audit cycle).

        Writes inventory caches and emits/sweeps signals just like the
        audit-driven run. Returns the run summary.
        """
        try:
            from mcp_admin import monitor as _mcp_monitor
        except ImportError:
            return jsonify({"ok": False, "error": "mcp module not importable"}), 500
        shared = _shared_dir()
        bots = _all_bots()
        try:
            result = _mcp_monitor.run(shared, bots, load_network(network_path))
            return jsonify({
                "ok": True,
                "bots_checked": result["bots_checked"],
                "findings_count": len(result["findings"]),
                "swept_resolved": result["swept_resolved"],
            })
        except Exception as e:
            return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500

    @app.get("/api/mcp-admin/catalog")
    def api_mcp_admin_catalog() -> Response:
        """Return the pod's MCP server catalog (vetted, available for install).

        Bootstraps the default catalog on first read (Phase B seed: GitHub MCP
        + Filesystem MCP). Edits to the catalog flow through proposals in a
        later phase; for now the catalog is read-only via this endpoint.
        """
        try:
            from mcp_admin import catalog as _cat
        except ImportError:
            return jsonify({"ok": False, "error": "mcp module not importable"}), 500
        shared = _shared_dir()
        _cat.write_default_if_missing(shared)
        cat = _cat.load(shared)
        return jsonify(cat.to_dict())

    def _create_mcp_proposal(action_kind: str, action_payload: dict, bot_id: str, summary: str):
        """Create + auto-apply an operator-originated MCP proposal.

        Returns (proposal_dict, None) — read ``proposal_dict["status"]`` for
        the apply outcome. Returns (None, err) on creation-class failure.
        """
        # Lazy import from server — cycle-safe because server imports this
        # module only inside the create_app() function body.
        from .server import _operator_create_apply
        try:
            from schema.proposal import RiskTag
        except ImportError as exc:
            return None, f"schema import failed: {exc}"
        risk = RiskTag(
            blast_radius="bot",
            reversibility="auto",
            touches=["bot_config"],
        )
        return _operator_create_apply(
            action_kind=action_kind,
            action_payload=action_payload,
            bot_id=bot_id,
            summary=summary,
            technique="operator_ui_install",
            dimension="operational_health",
            risk=risk,
            shared_dir=_shared_dir(),
        )

    @app.post("/api/mcp-admin/install")
    def api_mcp_admin_install() -> Response:
        """Create a pending InstallMcpServer proposal for the operator's approval.

        Body:
          bot_id, server_id, catalog_id    required
          env_bindings: {VAR: "keystore:key"}    required (may be empty)
          token_values: {keystore_key: value}    optional — when provided, each
            (slot, value) pair is written to the keystore BEFORE the proposal
            is created. Lets operators paste a PAT inline at install time
            instead of dropping to the CLI to register the slot separately.
          extra_args: [str, ...]    optional — positional args appended to the
            catalog entry's args at install time. Used by per-skill wrappers
            that bind a vetted catalog server to a user-supplied scope (e.g.
            the Obsidian skill installs catalog_id="filesystem" with
            extra_args=[vault_path] so the bot's filesystem MCP is scoped to
            the vault dir). Flows through ``mcp.servers[server_id].args`` and
            the wrapper script's trailing ``"$@"`` expansion.

        Token-write validation:
          - Each key in token_values MUST be referenced by some env binding
            (no orphan writes — prevents the modal from sneaking unrelated
            secrets into the keystore).
          - Each value must be non-empty, no whitespace, ≤ 200 chars.
          - Provider for register() is derived from catalog_id (the catalog
            entry's slug — "github", "brave", etc.); scope is "shared".

        Returns the standard operator-proposal-response shape, with an extra
        ``tokens_saved`` field when token_values was processed.
        """
        from .server import _operator_proposal_response
        data = (request.get_json(silent=True) or {})
        bot_id = (data.get("bot_id") or "").strip()
        server_id = (data.get("server_id") or "").strip()
        catalog_id = (data.get("catalog_id") or "").strip()
        env_bindings = data.get("env_bindings") or {}
        token_values = data.get("token_values") or {}
        extra_args = data.get("extra_args") or []
        if not (bot_id and server_id and catalog_id):
            return jsonify({"ok": False, "error": "bot_id, server_id, catalog_id are required"}), 400
        if not isinstance(env_bindings, dict):
            return jsonify({"ok": False, "error": "env_bindings must be an object"}), 400
        if not isinstance(token_values, dict):
            return jsonify({"ok": False, "error": "token_values must be an object"}), 400
        if not isinstance(extra_args, list) or not all(isinstance(a, str) for a in extra_args):
            return jsonify({"ok": False, "error": "extra_args must be a list of strings"}), 400

        # Write tokens to keystore FIRST so the InstallMcpServer proposal that
        # follows can reference the now-existing slots. If any token-write
        # fails, abort BEFORE creating the proposal — better to surface the
        # token error than to ship an MCP install pointing at a missing slot.
        tokens_saved = 0
        if token_values:
            # Build the set of slot names actually referenced by env_bindings.
            # env_bindings values look like "keystore:slot-name" — we extract
            # the slot from each value so token_values keys can be checked
            # against this set (no orphan writes).
            referenced_slots: set[str] = set()
            for v in env_bindings.values():
                if isinstance(v, str) and v.startswith("keystore:"):
                    referenced_slots.add(v[len("keystore:"):])

            for slot, value in token_values.items():
                if not isinstance(slot, str) or not slot.strip():
                    return jsonify({"ok": False, "error": "token_values keys must be non-empty strings"}), 400
                if slot not in referenced_slots:
                    return jsonify({
                        "ok": False,
                        "error": (
                            f"token_values key '{slot}' is not referenced by any "
                            "env binding — refusing to write an orphan secret"
                        ),
                    }), 400
                if not isinstance(value, str) or not value.strip():
                    return jsonify({"ok": False, "error": f"token value for '{slot}' is empty"}), 400
                token = value.strip()
                if len(token) > 200 or any(c.isspace() for c in token):
                    return jsonify({
                        "ok": False,
                        "error": f"token value for '{slot}' looks malformed (whitespace or too long)",
                    }), 400

            try:
                from evolve_admin.keystore import KeystoreManager
                mgr = KeystoreManager(_shared_dir())
                for slot, value in token_values.items():
                    token = value.strip()
                    existing = mgr.ks.get_key_entry(slot)
                    if not existing:
                        mgr.register(
                            slot,
                            provider=catalog_id,
                            scope="shared",
                            description=f"Token for MCP server {server_id} ({catalog_id})",
                            bots=None,
                            value=token,
                        )
                    else:
                        mgr.set_value(slot, token)
                    tokens_saved += 1
            except Exception as exc:  # noqa: BLE001
                return jsonify({"ok": False, "error": f"failed to save token: {exc}"}), 500

        summary = f"Install MCP server {server_id!r} ({catalog_id}) on {bot_id}"
        action_payload = {
            "bot_id": bot_id,
            "server_id": server_id,
            "catalog_id": catalog_id,
            "env_bindings": env_bindings,
        }
        if extra_args:
            action_payload["extra_args"] = list(extra_args)
        proposal, err = _create_mcp_proposal(
            "InstallMcpServer",
            action_payload,
            bot_id=bot_id,
            summary=summary,
        )
        resp = _operator_proposal_response(proposal, err)
        # _operator_proposal_response returns (response, status) or a Flask
        # Response. Annotate tokens_saved when we got a successful response
        # tuple so the UI can confirm "1 token saved + install applied".
        if tokens_saved and isinstance(resp, tuple) and len(resp) == 2:
            body, status = resp
            try:
                payload = body.get_json() if hasattr(body, "get_json") else None
                if isinstance(payload, dict):
                    payload["tokens_saved"] = tokens_saved
                    return jsonify(payload), status
            except Exception:  # noqa: BLE001
                pass
        elif tokens_saved and hasattr(resp, "get_json"):
            try:
                payload = resp.get_json()
                if isinstance(payload, dict):
                    payload["tokens_saved"] = tokens_saved
                    return jsonify(payload)
            except Exception:  # noqa: BLE001
                pass
        return resp

    @app.post("/api/mcp-admin/remove")
    def api_mcp_admin_remove() -> Response:
        """Create a pending RemoveMcpServer proposal."""
        from .server import _operator_proposal_response
        data = (request.get_json(silent=True) or {})
        bot_id = (data.get("bot_id") or "").strip()
        server_id = (data.get("server_id") or "").strip()
        if not (bot_id and server_id):
            return jsonify({"ok": False, "error": "bot_id and server_id required"}), 400
        summary = f"Remove MCP server {server_id!r} from {bot_id}"
        proposal, err = _create_mcp_proposal(
            "RemoveMcpServer",
            {"bot_id": bot_id, "server_id": server_id},
            bot_id=bot_id,
            summary=summary,
        )
        return _operator_proposal_response(proposal, err)

    @app.post("/api/mcp-admin/update")
    def api_mcp_admin_update() -> Response:
        """Create a pending UpdateMcpServerConfig proposal (env-binding mutation)."""
        from .server import _operator_proposal_response
        data = (request.get_json(silent=True) or {})
        bot_id = (data.get("bot_id") or "").strip()
        server_id = (data.get("server_id") or "").strip()
        env_bindings = data.get("env_bindings") or {}
        if not (bot_id and server_id):
            return jsonify({"ok": False, "error": "bot_id and server_id required"}), 400
        if not isinstance(env_bindings, dict):
            return jsonify({"ok": False, "error": "env_bindings must be an object"}), 400
        summary = f"Update env bindings for MCP server {server_id!r} on {bot_id}"
        proposal, err = _create_mcp_proposal(
            "UpdateMcpServerConfig",
            {"bot_id": bot_id, "server_id": server_id, "env_bindings": env_bindings},
            bot_id=bot_id,
            summary=summary,
        )
        return _operator_proposal_response(proposal, err)
