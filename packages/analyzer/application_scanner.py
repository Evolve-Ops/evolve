"""
application_scanner.py — Standalone application discovery script.

Designed to run as the bot user:
    sudo -u admin_bot python3 /path/to/application_scanner.py --bot admin_bot

Scans the bot's own workspace and writes manifests to:
    ~/.openclaw/workspace/manifests/{app_id}.json

Also writes scan status to the shared dir so the Evolve admin UI can poll progress:
    /Users/Shared/evolve/applications/{bot_id}/.scan-status.json

Usage:
    python3 application_scanner.py --bot <bot_id> [--no-llm] [--shared-dir <path>]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Evolve application scanner")
    parser.add_argument("--bot", required=True, help="Bot ID")
    parser.add_argument("--user", default=None, help="OS username, when different from bot ID")
    parser.add_argument("--no-llm", action="store_true", help="Skip LLM, fast evidence only")
    parser.add_argument(
        "--shared-dir",
        default="/Users/Shared/evolve",
        help="Shared Evolve directory (default: /Users/Shared/evolve)",
    )
    args = parser.parse_args()

    bot_id: str = args.bot
    os_user: str = args.user or bot_id
    use_llm: bool = not args.no_llm
    shared_dir = Path(args.shared_dir)

    # Bot workspace — where this script is designed to run as the bot user.
    # Platform-keyed home resolution (/Users on macOS, /home on Linux);
    # a hardcoded /Users/ made this fail "workspace not found" on Linux. (W10-G #5.)
    from evolve_config import user_home
    workspace = user_home(os_user) / ".openclaw" / "workspace"
    if not workspace.exists():
        print(f"ERROR: workspace not found: {workspace}", file=sys.stderr)
        sys.exit(1)

    # Output directory — manifests live in the bot's own workspace.
    # The admin server reads them via `sudo -u {bot_id} cat`.
    output_dir = workspace / "manifests"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load network config for openclaw_cmd resolution
    network_path = shared_dir / "network.json"
    config: dict = {}
    if network_path.exists():
        try:
            config = json.loads(network_path.read_text())
        except Exception:
            pass

    from evolve_admin.applications.scanner import scan_workspace_pipeline  # type: ignore

    print(f"[scanner] Starting scan for {bot_id} — workspace: {workspace}", flush=True)
    print(f"[scanner] Manifests output: {output_dir}", flush=True)
    print(f"[scanner] LLM: {'enabled' if use_llm else 'disabled'}", flush=True)

    results = scan_workspace_pipeline(
        workspace=workspace,
        bot_id=bot_id,
        shared_dir=shared_dir,
        config=config,
        use_llm=use_llm,
        output_dir=output_dir,
        user=os_user,
    )

    print(
        f"[scanner] Done — {len(results)} application(s) discovered/updated",
        flush=True,
    )
    for r in results:
        print(f"  - {r.get('name', r.get('id', '?'))} ({r.get('id', '?')})", flush=True)


if __name__ == "__main__":
    main()
