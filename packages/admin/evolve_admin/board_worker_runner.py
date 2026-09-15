"""board_worker_runner — long-running daemon entry point for board_worker.

Watches every bot's ``{shared_dir}/boards/<bot>/events/`` for a wake event
(D-BI4 §1) and dispatches a bounded worker per card. Same shape as
``packages/analyzer/signal_subscriber_runner.py`` — see that module and
``board_worker.py``'s module docstring for the pattern this mirrors.

Not yet wired into ``evolve-admin install-infra-jobs`` — see board_worker.py
deviation 6 (deploy.py/cli.py are both at their file-size-ratchet ceiling).
Run manually for now:

    python3 -m evolve_admin.board_worker_runner --shared-dir /Users/Shared/evolve

The production model client is intentionally NOT wired here yet either — the
brief is explicit that this PR "claims no live delegation." Passing
``--dry-run`` (the default) runs the poll loop with a model client that
raises if ever called, so a misconfigured pod fails loudly on the first
``kind: llm`` action rather than spending against a client nobody reviewed.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal as _os_signal
import sys
from pathlib import Path

from evolve_config import CANONICAL_SHARED_DIR, resolve_network_path

logger = logging.getLogger("evolve_admin.board_worker_runner")


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
        logger.warning("board-worker: cannot read network %s: %s", network_path, exc)
        return {}


def _refusing_model_client(*, prompt: str, context: dict):
    raise RuntimeError(
        "board_worker_runner has no model client wired yet — this pod would "
        "need a reviewed client passed to run_loop() before any kind:llm "
        "action can execute. See board_worker.py deviation 6.")


def main(argv: list[str] | None = None) -> int:
    _setup_logging()

    parser = argparse.ArgumentParser(
        description="Watch board events and dispatch bounded per-card workers.")
    parser.add_argument(
        "--shared-dir", type=Path,
        default=Path(os.environ.get("EVOLVE_SHARED", str(CANONICAL_SHARED_DIR))))
    parser.add_argument(
        "--network", type=Path,
        default=Path(os.environ.get("EVOLVE_NETWORK", str(resolve_network_path()))))
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--stop-after", type=float, default=None)
    args = parser.parse_args(argv)

    from evolve_admin.board_worker import run_loop

    network = _load_network(args.network)

    def _term(signum: int, _frame: object) -> None:  # noqa: ARG001
        logger.info("board-worker: received signal %d, exiting", signum)
        sys.exit(0)

    _os_signal.signal(_os_signal.SIGTERM, _term)
    _os_signal.signal(_os_signal.SIGINT, _term)

    try:
        run_loop(
            args.shared_dir, network, model_client=_refusing_model_client,
            poll_interval_seconds=args.poll_interval,
            stop_after_seconds=args.stop_after,
        )
    except KeyboardInterrupt:
        logger.info("board-worker: KeyboardInterrupt, exiting")
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("board-worker: run_loop crashed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
