#!/usr/bin/env python3
"""check-release-notes.py — Nudge for missing RELEASES.yaml entries.

Warns (does not block) when a PR titled ``feat:`` or ``security:`` lands
without a corresponding entry in ``RELEASES.yaml``. See
``docs/spec-release-tiers-2026-05-16.md`` for the policy.

The check is intentionally lenient:

- Direct pushes to main (no PR title) → skipped entirely.
- ``fix:``, ``chore:``, ``docs:``, ``refactor:`` → not flagged (maintenance is fine).
- ``feat:`` / ``security:`` PRs → require a new entry with matching tier.

Usage:
    python3 scripts/check-release-notes.py --pr-title "feat(banner): tiers"
    python3 scripts/check-release-notes.py  # uses $PR_TITLE / $GITHUB_PR_TITLE

Exit codes:
    0 — no issue
    1 — warning emitted (CI may flip to blocking later)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASES_FILE = REPO_ROOT / "RELEASES.yaml"

PREFIX_TO_TIER = {
    "security": "security",
    "feat":     "feature",
}

# Conventional-commit prefix pattern: `prefix(scope)!?:` or `prefix!?:`.
_PREFIX_RE = re.compile(r"^([a-z]+)(?:\([^)]+\))?!?:\s")


def _classify(pr_title: str) -> str | None:
    """Return the tier a PR title implies, or None if it doesn't claim one."""
    m = _PREFIX_RE.match(pr_title.strip())
    if not m:
        return None
    return PREFIX_TO_TIER.get(m.group(1).lower())


def _load_yaml_versions() -> set[str]:
    """Return the set of version strings present in RELEASES.yaml."""
    if not RELEASES_FILE.exists():
        return set()
    try:
        import yaml  # type: ignore
    except ImportError:
        sys.stderr.write("[check-release-notes] PyYAML not installed; skipping check.\n")
        return set()
    try:
        data = yaml.safe_load(RELEASES_FILE.read_text(encoding="utf-8")) or []
    except Exception as e:
        sys.stderr.write(f"[check-release-notes] could not parse RELEASES.yaml: {e}\n")
        return set()
    if not isinstance(data, list):
        return set()
    return {
        str(e.get("version"))
        for e in data
        if isinstance(e, dict) and e.get("version")
    }


def _file_changed() -> bool:
    """Best-effort: does the PR touch RELEASES.yaml? Falls back to True on error."""
    try:
        import subprocess
        base = os.environ.get("GITHUB_BASE_REF") or "origin/main"
        out = subprocess.run(
            ["git", "diff", "--name-only", f"{base}...HEAD"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return True  # can't tell — don't false-flag
        return "RELEASES.yaml" in out.stdout
    except Exception:
        return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr-title", default=os.environ.get("PR_TITLE") or os.environ.get("GITHUB_PR_TITLE", ""))
    args = parser.parse_args()
    pr_title = args.pr_title.strip()

    if not pr_title:
        print("[check-release-notes] no PR title (direct push?); skipping.")
        return 0

    implied_tier = _classify(pr_title)
    if implied_tier is None:
        print(f"[check-release-notes] PR title '{pr_title}' implies maintenance tier; no entry required.")
        return 0

    if _file_changed():
        print(f"[check-release-notes] RELEASES.yaml updated for {implied_tier} PR — ok.")
        return 0

    versions = _load_yaml_versions()
    print(
        f"::warning::PR title '{pr_title}' implies a '{implied_tier}' release, but "
        f"RELEASES.yaml was not updated. Add an entry so the upgrade banner can "
        f"render it. ({len(versions)} entries currently tracked.) "
        f"See docs/spec-release-tiers-2026-05-16.md."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
