#!/usr/bin/env python3
"""Signal-store transition notifier — Phase 4 LaunchDaemon entrypoint.

One tick per invocation. Reads firing/resolved Signals from the store
and pushes transitions to the operator's chat channel via the alert
dispatcher (Phase 1). Default-off in v1; opt in via the admin UI's
``alerts.signal_notifier.enabled`` toggle.

Schedule: every 60s via ``ai.evolve.evolve.signal-notifier`` LaunchDaemon
(see ``_install_launchd_signal_notifier`` in
``packages/admin/evolve_admin/deploy.py``).
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from evolve_admin.alerts.signal_notifier import run_once


def _load_network(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def main(argv: list[str] | None = None) -> int:
    print(
        f"[signal-notifier] heartbeat ok @ {datetime.now(timezone.utc).isoformat()}",
        flush=True,
    )
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--network",
        default="/Users/Shared/evolve/network.json",
        help="Path to network.json (default: /Users/Shared/evolve/network.json).",
    )
    ap.add_argument(
        "--shared-dir",
        default=None,
        help="Override shared dir (defaults to network.json::sharedDir).",
    )
    args = ap.parse_args(argv)

    network = _load_network(Path(args.network))
    shared = Path(args.shared_dir) if args.shared_dir else Path(
        network.get("sharedDir", "/Users/Shared/evolve")
    )
    run_once(shared_dir=shared, network=network)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
