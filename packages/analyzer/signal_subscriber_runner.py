"""signal_subscriber_runner — Long-running daemon entry point.

Watches ``{shared_dir}/signals/firing/`` and invokes any generator whose
charter declares ``subscribes_to: [<signal_type>, ...]`` whenever a
matching Signal lands. Closes the latency gap between Signal arrival and
generator response — without this, an acute Signal landing two hours
after the daily sweep had to wait ~22h for the next sweep to act on it.

Run as the ``evolve`` user under the launchd plist
``ai.evolve.evolve.signal-subscriber``. Logs to
``/Users/Shared/evolve/logs/signal-subscriber.log`` and ``.err.log``.

To disable: bootout the plist. The daily sweep continues to handle
all subscribers as a safety net.

Spec: internal/spec-signal-subscriber-2026-05-31.md.
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

logger = logging.getLogger("evolve_admin.signal_subscriber_runner")


def _setup_logging() -> None:
    """Best-effort root logger config so launchd's StandardOutPath captures
    our messages even before a structured logging config is loaded."""
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _load_network(network_path: Path) -> dict:
    """Read network.json. Missing/malformed file returns an empty dict.

    The subscriber daemon should not refuse to start because a network
    edit is mid-flight — better to log and run with what we have. The
    map refresh on the next subscription tick will pick up the new
    config once it's parseable.
    """
    try:
        return json.loads(network_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("signal-subscriber: cannot read network %s: %s", network_path, exc)
        return {}


def main(argv: list[str] | None = None) -> int:
    _setup_logging()

    parser = argparse.ArgumentParser(
        description="Watch the Signal store and dispatch subscribed generators on Signal arrival.",
    )
    parser.add_argument(
        "--shared-dir",
        type=Path,
        default=Path(os.environ.get("EVOLVE_SHARED", str(CANONICAL_SHARED_DIR))),
        help="Pod-wide shared dir (default: platform-keyed canonical shared dir)",
    )
    parser.add_argument(
        "--network",
        type=Path,
        default=Path(os.environ.get("EVOLVE_NETWORK", str(resolve_network_path()))),
        help="Path to network.json (default: platform-keyed canonical path)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="Seconds between firing/ scans (default: 1.0)",
    )
    parser.add_argument(
        "--stop-after",
        type=float,
        default=None,
        help="If set, exit after this many seconds (used by tests / smoke runs).",
    )
    args = parser.parse_args(argv)

    analyzer_dir = Path(__file__).resolve().parent

    # Late import so `--help` works without loading the subscriber stack.
    from signals.subscriber import run_loop

    network_config = _load_network(args.network)

    # Handle SIGTERM gracefully — launchd uses SIGTERM by default at bootout.
    # Without this we'd take the default Python KeyboardInterrupt-equivalent
    # behaviour and bury a traceback in the .err.log.
    def _term(signum: int, _frame: object) -> None:  # noqa: ARG001
        logger.info("signal-subscriber: received signal %d, exiting", signum)
        # Re-raise as SystemExit so the run_loop's polling sleep gets
        # interrupted on the next tick.
        sys.exit(0)

    _os_signal.signal(_os_signal.SIGTERM, _term)
    _os_signal.signal(_os_signal.SIGINT, _term)

    generators_code_dir = analyzer_dir / "generators"
    records_dir = args.shared_dir / "generators"

    try:
        run_loop(
            args.shared_dir,
            network_config=network_config,
            generators_code_dir=generators_code_dir,
            records_dir=records_dir,
            poll_interval_seconds=args.poll_interval,
            stop_after_seconds=args.stop_after,
        )
    except KeyboardInterrupt:
        logger.info("signal-subscriber: KeyboardInterrupt, exiting")
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("signal-subscriber: run_loop crashed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
