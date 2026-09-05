"""
evolve_admin.mcp_bridge — Entry point.

Usage:
    python -m evolve_admin.mcp_bridge [options]

Options:
    --network PATH    Path to network.json (default: the platform-keyed
                      DEFAULT_NETWORK_CONFIG — /Users/Shared/evolve on macOS,
                      /var/lib/evolve on Linux)
    --port PORT       HTTP port to listen on (default: from network.json mcp_bridge.port, or 5051)
    --host HOST       Bind address (default: 0.0.0.0)
    --log-level LEVEL Python log level (default: INFO)

The bridge reads its full configuration from the mcp_bridge section of network.json.
CLI flags override those values for development/testing.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from pathlib import Path

# ── Log rotation policy ───────────────────────────────────────────────────────
#
# The daemon's stdout/stderr is captured by launchd into
# ``~/.evolve/logs/mcp-bridge.log`` (deploy.py / mcp_service.py both point the
# LaunchDaemon ``StandardOut/ErrorPath`` there). launchd has NO rotation for
# that path, and the bridge previously routed every log line to stderr via
# ``basicConfig`` — so the file grew without bound (observed 214 MB on the
# mini). We now own the file with a ``RotatingFileHandler`` so it self-caps,
# the same treatment the admin server's own log already gets (telemetry.py).
# 10 MiB × 3 backups ≈ a 40 MiB ceiling.
_LOG_FILENAME = "mcp-bridge.log"
_LOG_MAX_BYTES = 10 * 1024 * 1024
_LOG_BACKUP_COUNT = 3
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S"


def _bridge_log_path() -> Path:
    """The bridge's own log file — ``~/.evolve/logs/mcp-bridge.log`` for the
    account the daemon runs as (``/Users/evolve`` under the LaunchDaemon,
    ``/home/evolve`` on Linux).

    Resolved via ``expanduser`` to match the audit-log path resolution in
    :func:`main` and the launchd ``StandardOutPath`` the daemon's stdout is
    captured into (deploy.py / mcp_service.py both target evolve's
    ``~/.evolve/logs/mcp-bridge.log``). Writing the rotated file to the SAME
    path keeps ``evolve-admin mcp-bridge logs`` / ``tail_logs`` working
    unchanged — only now the file self-caps.
    """
    return Path("~/.evolve/logs").expanduser() / _LOG_FILENAME


def _configure_logging(level: int) -> None:
    """Route the bridge's logging through a size-capped RotatingFileHandler.

    Replaces the previous stderr ``basicConfig``. Under the LaunchDaemon,
    stderr is redirected by launchd into ``mcp-bridge.log`` — a path launchd
    never rotates — so the bridge's own log lines (and uvicorn's, via
    propagation) grew it without bound. The handler now owns that file and caps
    it at ``_LOG_MAX_BYTES`` × ``_LOG_BACKUP_COUNT``.

    The handler attaches to the ROOT logger because the bridge spans two logger
    hierarchies (``evolve.mcp_bridge.*`` and ``evolve_admin.mcp_bridge.*``) plus
    uvicorn/starlette — root is their common ancestor (same scope ``basicConfig``
    configured before). The daemon adds NO stderr handler (its stderr is the
    launchd-captured file we just capped; re-emitting there would re-bloat it);
    an interactive tty run DOES get a console handler so manual
    ``python -m evolve_admin.mcp_bridge`` debugging still prints.

    Perms/ACL: the file is created by the ``evolve`` daemon inside
    ``~/.evolve/logs`` (the deploy-managed dir that carries evolve's inherited
    read ACL), so the new file inherits that ACL and evolve ownership — no
    chmod needed, and mcp-bridge.log is a diagnostic log, not a secret.

    Falls back to stderr-only ``basicConfig`` if the log file can't be opened
    (e.g. a dev box without a writable ``~/.evolve``) so logging never silently
    drops.
    """
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT)
    path: Path | None = None
    try:
        path = _bridge_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError:
        logging.basicConfig(level=level, format=_LOG_FORMAT, datefmt=_LOG_DATEFMT)
        logging.getLogger("evolve.mcp_bridge").warning(
            "Could not open rotating log at %s; logging to stderr only.",
            path, exc_info=True,
        )
        return

    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    handlers: list[logging.Handler] = [file_handler]
    # Interactive (tty) runs also echo to the console; the daemon (no tty) does
    # not, so its launchd-captured stderr stays idle instead of re-bloating the
    # very file we just capped.
    if sys.stderr is not None and sys.stderr.isatty():
        console = logging.StreamHandler()
        console.setLevel(level)
        console.setFormatter(formatter)
        handlers.append(console)

    root = logging.getLogger()
    root.setLevel(level)
    # Idempotent (re-entry / tests): replace any handlers we'd otherwise duplicate.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    for handler in handlers:
        root.addHandler(handler)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="evolve-mcp-bridge",
        description="Evolve MCP Bridge — expose pod context to Claude Desktop",
    )
    parser.add_argument(
        "--network",
        type=Path,
        default=None,
        help="Path to network.json (default: platform-keyed DEFAULT_NETWORK_CONFIG)",
    )
    parser.add_argument("--port", type=int, default=None, help="HTTP port (default: 5051)")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level",
    )
    args = parser.parse_args()

    _configure_logging(getattr(logging, args.log_level))
    log = logging.getLogger("evolve.mcp_bridge")

    # ── Load config ───────────────────────────────────────────────────────────
    from evolve_admin.config import DEFAULT_NETWORK_CONFIG, load_network

    network_path = args.network or DEFAULT_NETWORK_CONFIG
    try:
        net = load_network(network_path)
    except RuntimeError as e:
        log.error("Cannot load network config: %s", e)
        sys.exit(1)

    mcp_cfg: dict = net.get("mcp_bridge", {})

    # W10-F #5: default-ON must match the INSTALL side. deploy.py installs
    # this daemon whenever mcp_bridge is absent or not explicitly disabled
    # (`_mcp_cfg.get("enabled", True)`). A fresh pod's network.json has no
    # mcp_bridge section at all, so a default-False here meant "installed but
    # exit(1) on every start" → the unit flapped exit-1 forever and nothing
    # bound :5051. The two defaults are now the same: absent ⇒ enabled.
    if not mcp_cfg.get("enabled", True):
        # exit(0) avoids a `failed` state / error-log spam, but the unit is
        # keep_alive (Restart=always) so a bridge disabled AFTER install still
        # restart-loops quietly — deploy skips install when disabled but has no
        # uninstall-on-disable path. Fully stopping it needs that or
        # Restart=on-failure for this daemon (W10-F follow-up #4).
        log.info(
            "MCP bridge is explicitly disabled in network.json "
            "(mcp_bridge.enabled = false); exiting cleanly."
        )
        sys.exit(0)

    port = args.port or mcp_cfg.get("port", 5051)
    auth_mode = mcp_cfg.get("auth", {}).get("mode", "tailscale")
    api_key = mcp_cfg.get("auth", {}).get("api_key")
    audit_log_path = Path(
        mcp_cfg.get("audit_log", "~/.evolve/logs/mcp-audit.jsonl")
    ).expanduser()

    log.info("Starting Evolve MCP Bridge on %s:%d", args.host, port)
    log.info("Network config: %s", network_path)
    log.info("Auth mode: %s", auth_mode)
    log.info("Audit log: %s", audit_log_path)

    # ── Build components ──────────────────────────────────────────────────────
    from .audit import AuditLog
    from .auth import Auth
    from .registry import BotRegistry
    from .server import build_starlette_app

    # W10-G #4: build + serve under one guard. systemd marks a KeepAlive unit
    # "active (running)" the instant the process forks, so a crash here (a
    # missing serving dep, a bind error) showed up only as a flapping
    # "activating" unit that never bound :5051 — with the real cause buried.
    # Log the cause explicitly (and to the StandardError path) before exiting
    # so the failure is unambiguous instead of a bare traceback.
    try:
        registry = BotRegistry(network_path=network_path, mcp_config=mcp_cfg)
        log.info("Bots registered: %s", registry.bot_ids())

        if not registry.bot_ids():
            log.warning(
                "No bots found in network.json. "
                "The bridge will start but list_bots will return empty. "
                "Provision bots via Evolve and the bridge will detect them automatically."
            )

        registry.on_change(
            lambda added, removed: log.info(
                "Bot registry updated — added: %s, removed: %s", added, removed
            )
        )

        audit = AuditLog(audit_log_path)
        auth = Auth(mode=auth_mode, api_key=api_key)

        app = build_starlette_app(registry, audit, auth, port=port)

        # ── Serve ───────────────────────────────────────────────────────────
        import uvicorn

        uvicorn.run(
            app,
            host=args.host,
            port=port,
            log_level=args.log_level.lower(),
            access_log=False,  # MCP audit log covers access logging
            # Don't let uvicorn install its own stderr handlers — that stderr
            # is the launchd-captured mcp-bridge.log we just rate-capped. With
            # log_config=None, uvicorn's loggers propagate to root and flow
            # through the RotatingFileHandler instead.
            log_config=None,
        )
    except Exception:
        log.critical(
            "MCP bridge failed to start (host=%s port=%s) — see traceback.",
            args.host, port, exc_info=True,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
