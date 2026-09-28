"""HTTP routes for the MCP bridge management surface.

/api/mcp/*   Evolve→Claude-Desktop MCP bridge — status, install, uninstall,
             start, stop, restart, reload, logs, audit-log, config-snippet,
             config-save, bot-access, agents-check, inject-handoff,
             write-claude-md.

Extracted from server.py (Batch E, Step 3).
"""

from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, request, Response

from ..config import load_network, bot_home as _bot_home
from ..telemetry import get_logger

_log = get_logger("web.routes_mcp")


def _register_mcp_routes(app: Flask, network_path: Path) -> None:
    """Register /api/mcp/* endpoints for MCP bridge management."""
    from .. import mcp_service as _mcp

    @app.get("/api/mcp/status")
    def api_mcp_status() -> Response:
        s = _mcp.status()
        # Enrich with config from network.json
        from ..config import load_network
        net = load_network(network_path)
        mcp_cfg = net.get("mcp_bridge", {})
        s["enabled"] = mcp_cfg.get("enabled", False)
        s["port"] = mcp_cfg.get("port", 5051)
        s["primary_bot"] = mcp_cfg.get("primary_context_bot") or net.get("primary")
        s["tailscale_hostname"] = mcp_cfg.get("tailscale_hostname", "")
        s["auth_mode"] = mcp_cfg.get("auth", {}).get("mode", "tailscale")
        s["bots"] = list(net.get("bots", {}).keys())
        return jsonify(s)

    @app.post("/api/mcp/install")
    def api_mcp_install() -> Response:
        from ..config import load_network
        net = load_network(network_path)
        mcp_cfg = net.get("mcp_bridge", {})
        port = mcp_cfg.get("port", 5051)
        ok, msg = _mcp.install(port=port, network=network_path)
        return jsonify({"ok": ok, "message": msg})

    @app.post("/api/mcp/uninstall")
    def api_mcp_uninstall() -> Response:
        ok, msg = _mcp.uninstall()
        return jsonify({"ok": ok, "message": msg})

    @app.post("/api/mcp/start")
    def api_mcp_start() -> Response:
        ok, msg = _mcp.start()
        return jsonify({"ok": ok, "message": msg})

    @app.post("/api/mcp/stop")
    def api_mcp_stop() -> Response:
        ok, msg = _mcp.stop()
        return jsonify({"ok": ok, "message": msg})

    @app.post("/api/mcp/restart")
    def api_mcp_restart() -> Response:
        ok, msg = _mcp.restart()
        return jsonify({"ok": ok, "message": msg})

    @app.post("/api/mcp/reload")
    def api_mcp_reload() -> Response:
        """SIGHUP the bridge to reload bot registry without restart."""
        ok, msg = _mcp.reload_registry()
        return jsonify({"ok": ok, "message": msg})

    @app.get("/api/mcp/logs")
    def api_mcp_logs() -> Response:
        n = int(request.args.get("n", 100))
        lines = _mcp.tail_logs(min(n, 500))
        return jsonify({"lines": lines})

    @app.get("/api/mcp/audit-log")
    def api_mcp_audit_log() -> Response:
        from pathlib import Path as _Path
        from ..config import load_network
        net = load_network(network_path)
        mcp_cfg = net.get("mcp_bridge", {})
        audit_path = _Path(
            mcp_cfg.get("audit_log", "~/.evolve/logs/mcp-audit.jsonl")
        ).expanduser()
        writes_only = request.args.get("writes_only", "false").lower() == "true"
        n = int(request.args.get("n", 50))
        if not audit_path.exists():
            return jsonify({"entries": [], "count": 0})
        try:
            import json as _json
            lines = audit_path.read_text(encoding="utf-8").splitlines()
            entries = []
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = _json.loads(line)
                except Exception:
                    continue
                if writes_only and not entry.get("write"):
                    continue
                entries.append(entry)
                if len(entries) >= n:
                    break
            return jsonify({"entries": list(reversed(entries)), "count": len(entries)})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.get("/api/mcp/config-snippet")
    def api_mcp_config_snippet() -> Response:
        """Return the Claude Desktop JSON config snippet."""
        import json as _json
        from ..config import load_network
        net = load_network(network_path)
        mcp_cfg = net.get("mcp_bridge", {})
        port = mcp_cfg.get("port", 5051)
        tailscale_host = mcp_cfg.get("tailscale_hostname", "")
        auth_mode = mcp_cfg.get("auth", {}).get("mode", "tailscale")
        api_key = mcp_cfg.get("auth", {}).get("api_key")
        mode = request.args.get("mode", "local")  # "local" or "remote"

        if mode == "remote":
            url = f"http://{tailscale_host}:{port}/sse" if tailscale_host else f"http://<tailscale-hostname>:{port}/sse"
        else:
            url = f"http://localhost:{port}/sse"

        server_cfg: dict = {"url": url}
        if auth_mode == "api_key" and api_key:
            server_cfg["headers"] = {"Authorization": f"Bearer {api_key}"}

        snippet = {"mcpServers": {"evolve-pod": server_cfg}}
        return jsonify({
            "snippet": snippet,
            "snippet_json": _json.dumps(snippet, indent=2),
            "mode": mode,
            "url": url,
        })

    @app.post("/api/mcp/config")
    def api_mcp_config_save() -> Response:
        """Update mcp_bridge config in network.json."""
        from ..config import load_network, save_network
        body = request.get_json() or {}
        net = load_network(network_path)
        mcp_cfg = net.setdefault("mcp_bridge", {})

        if "enabled" in body:
            mcp_cfg["enabled"] = bool(body["enabled"])
        if "port" in body:
            mcp_cfg["port"] = int(body["port"])
        if "primary_context_bot" in body:
            mcp_cfg["primary_context_bot"] = body["primary_context_bot"]
        if "tailscale_hostname" in body:
            mcp_cfg["tailscale_hostname"] = body["tailscale_hostname"]
        if "auth_mode" in body:
            auth = mcp_cfg.setdefault("auth", {})
            auth["mode"] = body["auth_mode"]

        save_network(net, network_path)
        return jsonify({"ok": True})

    @app.get("/api/mcp/bot-access")
    def api_mcp_bot_access() -> Response:
        """Check workspace readability for each bot.

        The admin server runs as the 'evolve' user which has ACL read access to
        each bot's .openclaw/workspace directory (set by set_evolve_read_acl).
        Check directly — sudo -u <bot> is not available to the evolve daemon user.
        """
        import os as _os
        from ..config import load_network, get_bot_workspace
        net = load_network(network_path)
        bots_cfg = net.get("bots", {})
        primary = net.get("mcp_bridge", {}).get("primary_context_bot") or net.get("primary")
        results = []
        for bot_id in bots_cfg:
            ws = get_bot_workspace(bot_id)
            ws_str = str(ws) if ws is not None else str(_bot_home(bot_id, net) / ".openclaw" / "workspace")
            readable = _os.access(ws_str, _os.R_OK) and Path(ws_str).is_dir()
            results.append({
                "bot": bot_id,
                "workspace": ws_str,
                "readable": readable,
                "is_primary": bot_id == primary,
            })
        return jsonify({"bots": results, "primary": primary})

    @app.get("/api/mcp/agents-check")
    def api_mcp_agents_check() -> Response:
        """Check which bots have the MCP handoff instruction in their AGENTS.md."""
        from ..deploy import has_handoff_check
        net = load_network(network_path)
        bots_cfg = net.get("bots", {})
        results = {bot_id: has_handoff_check(bot_id) for bot_id in bots_cfg}
        return jsonify({"bots": results})

    @app.post("/api/mcp/inject-handoff/<bot_id>")
    def api_mcp_inject_handoff(bot_id: str) -> Response:
        """Inject the MCP handoff check into a specific bot's AGENTS.md."""
        from ..deploy import inject_handoff_check
        net = load_network(network_path)
        if bot_id not in net.get("bots", {}):
            return jsonify({"ok": False, "error": f"Unknown bot: {bot_id}"}), 400
        try:
            inject_handoff_check(bot_id)
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/api/mcp/write-claude-md")
    def api_mcp_write_claude_md() -> Response:
        """Regenerate ~/CLAUDE.md from current mcp_bridge config."""
        from .. import mcp_service as _mcp
        net = load_network(network_path)
        mcp_cfg = net.get("mcp_bridge", {})
        # Resolve primary context bot from config; fall back to the first
        # registered bot if no explicit primary is set. Never use a
        # hardcoded bot name here.
        primary_bot = (
            mcp_cfg.get("primary_context_bot")
            or net.get("primary")
            or next(iter(net.get("bots", {})), None)
        )
        if not primary_bot:
            return jsonify({
                "ok": False,
                "error": "No bots registered — add a bot first (evolve-admin add-bot)",
            }), 400
        port = mcp_cfg.get("port", 5051)
        tailscale_hostname = mcp_cfg.get("tailscale_hostname", "")
        try:
            path = _mcp.write_claude_md(primary_bot, port, tailscale_hostname)
            return jsonify({"ok": True, "path": str(path)})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
