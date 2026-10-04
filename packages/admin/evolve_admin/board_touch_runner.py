"""board_touch_runner — one-shot entry point for board_touch.sweep.

Not a long-running loop like ``board_worker_runner`` — D-TM3's scheduler is
a 5-minute StartInterval job (same shape as ``delivery_monitor.py``'s own
entry point), so this script runs one sweep and exits.

Not yet wired into ``evolve-admin install-infra-jobs`` — see
``board_touch.py``'s module docstring, deviation 2 (deploy.py/cli.py are
both at their file-size-ratchet ceiling). Run manually, or via cron/launchd
once wired, with:

    python3 -m evolve_admin.board_touch_runner --shared-dir /Users/Shared/evolve
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from evolve_config import CANONICAL_SHARED_DIR, resolve_network_path

logger = logging.getLogger("evolve_admin.board_touch_runner")


def _setup_logging() -> None:
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _load_network(network_path: Path) -> dict:
    try:
        return json.loads(network_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("board-touch: cannot read network %s: %s", network_path, exc)
        return {}


def main(argv: list[str] | None = None) -> int:
    _setup_logging()

    parser = argparse.ArgumentParser(
        description="Fire every due board touch (D-TM3) and ledger any miss.")
    parser.add_argument(
        "--shared-dir", type=Path,
        default=Path(os.environ.get("EVOLVE_SHARED", str(CANONICAL_SHARED_DIR))))
    parser.add_argument(
        "--network", type=Path,
        default=Path(os.environ.get("EVOLVE_NETWORK", str(resolve_network_path()))))
    args = parser.parse_args(argv)

    from evolve_admin.board_touch import sweep

    network = _load_network(args.network)
    try:
        results = sweep(args.shared_dir, network)
    except Exception:  # noqa: BLE001
        logger.exception("board-touch: sweep crashed")
        return 1
    if results:
        logger.info("[board_touch] fired %d touch(es)", len(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
