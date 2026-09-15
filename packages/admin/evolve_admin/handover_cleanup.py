"""Daily-cron entry point for pruning expired handover tokens.

Run via:

    python3 -m evolve_admin.handover_cleanup [--shared-dir PATH]

Idempotent; safe to schedule daily. Prints a one-line summary so cron
logs stay scannable.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import DEFAULT_SHARED_DIR, load_network
from .handover import cleanup_expired


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="evolve_admin.handover_cleanup",
        description="Prune expired handover tokens.",
    )
    p.add_argument(
        "--shared-dir",
        type=Path,
        default=None,
        help=(
            "Override shared dir. Defaults to network.json's sharedDir, "
            f"falling back to {DEFAULT_SHARED_DIR}."
        ),
    )
    args = p.parse_args(argv)

    shared_dir = args.shared_dir
    if shared_dir is None:
        try:
            net = load_network()
            shared_dir = Path(net.get("sharedDir") or DEFAULT_SHARED_DIR)
        except Exception:
            shared_dir = DEFAULT_SHARED_DIR

    result = cleanup_expired(shared_dir)
    print(
        f"handover_cleanup: removed={result['removed']} kept={result['kept']} "
        f"shared_dir={shared_dir}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
