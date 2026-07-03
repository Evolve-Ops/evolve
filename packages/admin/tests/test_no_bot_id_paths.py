"""Fail CI if any source file constructs /Users/{bot_id}/ directly.

bot_id (logical name in network.json) may differ from the macOS account name
(team_bot_b/personal_bot_user case). All path construction must go through:
  - evolve_admin.config.bot_home(bot_id) or
  - evolve_admin.config.get_bot_user(bot_id, network)

For the analyzer package:
  - evolve_config.bot_home(bot_id) or
  - evolve_config.get_bot_user(bot_id)

Direct f"/Users/{bot_id}/" construction silently works for 6 of 7 bots
(where bot_id == account) and silently breaks for team_bot_b.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# Source trees to lint
SRC_ROOTS = [
    REPO_ROOT / "packages" / "admin" / "evolve_admin",
    REPO_ROOT / "packages" / "analyzer",
    REPO_ROOT / "tools",
    REPO_ROOT / "docs" / "reference" / "capabilities" / "ea-pack" / "scripts",
]

# Matches direct f"/Users/{bot_id}" or "/Users/" + bot_id constructions
BAD_PATTERN = re.compile(r'f"/Users/\{bot_id\}|"/Users/" \+ bot_id')

# Account-creation sites and last-resort fallbacks that legitimately use bot_id.
# Each value is a list of line substrings that identify the allowlisted line.
# Every entry here must have a comment in source explaining why it's allowed.
ALLOWLIST: dict[str, list[str]] = {
    "setup_wizard.py": [
        # dscl account-creation commands — bot_id IS the new account name here
        'dscl", ".", "create", f"/Users/{bot_id}',
        'NFSHomeDirectory", f"/Users/{bot_id}',
        # _setup_oc_for_bot and helpers in account-creation context
        'return Path(f"/Users/{bot_id}/.openclaw/openclaw.json").exists()',
        'home = Path(f"/Users/{bot_id}")',
        'oc_path = Path(f"/Users/{bot_id}/.openclaw/openclaw.json")',
    ],
    # Analyzer package files use evolve_config.bot_home() as the primary path.
    # The f"/Users/{bot_id}" lines below are ONLY reachable as last-resort
    # fallbacks inside except (ImportError, KeyError) blocks — the primary
    # path always goes through evolve_config/pwd.getpwnam first.
    "defer_queue.py": [
        'return Path(f"/Users/{bot_id}/.openclaw/workspace/evolve")',
    ],
    "manifest_reflex_queue.py": [
        'return Path(f"/Users/{bot_id}/.openclaw/workspace/evolve")',
    ],
    "turn_detail.py": [
        'home = Path(f"/Users/{bot_id}")',
    ],
    "inventory.py": [
        'return Path(f"/Users/{bot_id}")',
    ],
    "scanner.py": [
        'return Path(f"/Users/{bot_id}")',
    ],
    "retire_orphan.py": [
        'return Path(f"/Users/{bot_id}/.openclaw/workspace")',
    ],
    "permissions.py": [
        'return Path(f"/Users/{bot_id}")',
    ],
    "tools.py": [
        'return Path(f"/Users/{bot_id}")',
    ],
    "run_eval.py": [
        'return Path(f"/Users/{bot_id}")',
    ],
    # ── Admin-side fallbacks inside except blocks ─────────────────────────────
    # Each of these is reachable only when the canonical bot_home() /
    # get_bot_workspace() lookup raises. The primary path is bot_home;
    # falling back to /Users/{bot_id} for a misconfigured bot is the
    # right behavior (best-effort instead of crash).
    "handover.py": [
        'home = Path(f"/Users/{bot_id}")',
    ],
    "manifest.py": [
        'workspace = Path(f"/Users/{bot_id}/.openclaw/workspace")',
    ],
    "connect.py": [
        'home = Path(f"/Users/{bot_id}")',
    ],
    # ── Analyzer fallbacks: same pattern as defer_queue / manifest_reflex_queue
    # The primary path is evolve_admin.config.bot_home(); fallback only fires
    # when the admin package isn't importable.
    "poller.py": [
        'self.state_path = Path(f"/Users/{bot_id}/.openclaw/workspace/evolve/{STATE_FILE_NAME}")',
        'cfg_path = Path(f"/Users/{bot_id}/{IMESSAGE_CONFIG_PATH}")',
    ],
    "observe.py": [
        'return Path(f"/Users/{bot_id}")',
    ],
}


def test_no_bot_id_paths_in_source():
    """No source file should construct /Users/{bot_id}/ directly."""
    offenders: list[str] = []

    for src_root in SRC_ROOTS:
        if not src_root.exists():
            continue
        for path in src_root.rglob("*.py"):
            # Skip test files (they mock things and don't ship to production)
            if "/tests/" in str(path) or path.name.startswith("test_"):
                continue

            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue

            for lineno, line in enumerate(lines, 1):
                if not BAD_PATTERN.search(line):
                    continue

                # Check allowlist
                base = path.name
                allowed_substrs = ALLOWLIST.get(base, [])
                if any(substr in line for substr in allowed_substrs):
                    continue

                # Not allowlisted — report it
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        "Direct /Users/{bot_id}/ path construction found. "
        "Use evolve_admin.config.bot_home(bot_id) or "
        "evolve_admin.config.get_bot_user(bot_id, network) instead.\n\n"
        "Affected lines:\n  " + "\n  ".join(offenders)
    )
