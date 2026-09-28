"""
Entry point for running the evolve-admin Flask server via python -m.

NOT the production launcher. The admin-ui LaunchDaemon invokes
`evolve-admin serve` (deploy.py `_admin_ui_jobspec` -> cli.py `serve`),
which already runs the app with threaded=True. This module is for:

  * ad-hoc local dev (`python -m evolve_admin.web.run`), and
  * the browser smoke harness, which spawns this same entry point as a
    subprocess (tests/browser/conftest.py).

Both call sites want the same threaded=True behavior as production: the
SPA shell issues ~50 synchronous <script> fetches on load, and a
single-threaded Werkzeug serializes them, producing janky local loads and
Page.goto timeouts under the smoke harness. Keep app.run() here in parity
with cli.py `serve`.

Usage:
    python -m evolve_admin.web.run [--host 127.0.0.1] [--port 5050] [--network PATH]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .server import create_app
from ..config import DEFAULT_NETWORK_CONFIG
from ..telemetry import setup_access_logging, setup_logging, get_logger


def main() -> None:
    parser = argparse.ArgumentParser(description="Evolve Admin web server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument("--port", type=int, default=5050, help="Port")
    parser.add_argument(
        "--network",
        default=str(DEFAULT_NETWORK_CONFIG),
        help="Path to network.json",
    )
    args = parser.parse_args()
    setup_logging()
    access_log = setup_access_logging()
    _log = get_logger("web.run")
    _log.info(
        "Starting evolve-admin server host=%s port=%d network=%s access_log=%s",
        args.host, args.port, args.network, access_log,
    )
    app = create_app(Path(args.network))
    # threaded=True mirrors cli.py `serve` (the production launcher) so local
    # dev and the browser smoke harness don't serialize the SPA's parallel
    # script fetches on Werkzeug's single-threaded default.
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
