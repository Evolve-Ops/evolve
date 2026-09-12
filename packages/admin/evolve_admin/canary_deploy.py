"""canary_deploy — deploy one bot from the code tree on PYTHONPATH.

Invoked by release_manager as::

    PYTHONPATH=<staging>/packages/admin:<staging>/packages/analyzer \
        /Users/Shared/evolve-venv/bin/python3 -m evolve_admin.canary_deploy \
        --bot <canary_bot> --network <network.json>

PYTHONPATH precedes the venv's PEP-660 editable install, so
``evolve_admin`` (and the analyzer packages deploy.py pulls in) resolve
from the staging worktree — the canary bot ends up running *candidate*
code while the fleet stays on stable. The same module run with the
fleet checkout on PYTHONPATH (or none — editable resolves to the fleet)
is the restore path.

Pod side effects are suppressed (``pod_side_effects=False``): candidate
code must not mutate pod-wide state (perm contracts, pod-level config)
before it has been promoted — it only gets to touch the canary bot.

Exit code 0 iff the deploy reported success. Output is the deploy log,
line-per-step, for the release tick to capture.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evolve_admin.canary_deploy")
    parser.add_argument("--bot", required=True)
    parser.add_argument("--network", required=True)
    args = parser.parse_args(argv)

    from evolve_admin import deploy as _deploy
    from evolve_admin.config import load_network

    network_path = Path(args.network)
    try:
        network = load_network(network_path)
    except Exception as e:
        print(f"FAIL: cannot load network.json: {e}", file=sys.stderr)
        return 1
    cfg = (network.get("bots") or {}).get(args.bot)
    if cfg is None:
        print(f"FAIL: bot {args.bot!r} not in network.json", file=sys.stderr)
        return 1

    result = _deploy.deploy_bot(
        args.bot,
        role=cfg.get("role") or "member",
        port=cfg.get("port"),
        network_path=network_path,
        dry_run=False,
        backup_repo_url=cfg.get("backupRepoUrl", ""),
        pod_side_effects=False,
    )
    for line in result.steps:
        print(line)
    if not result.success:
        detail = "; ".join(result.errors) or "deploy_bot reported failure"
        print(f"FAIL: {detail}", file=sys.stderr)
        return 1
    # Deliberately NO record_bot_deploy here: install.json is a pod-wide
    # file and this module runs CANDIDATE code during soaks (spec §2.4).
    # The release manager stamps via the running stable code after a
    # successful restore-from-fleet deploy (_restore_canary); during a
    # soak the stamp staying stale is fine — the canary is exempted from
    # the lagging-bot sweep by name, not by stamp.
    return 0


if __name__ == "__main__":
    sys.exit(main())
