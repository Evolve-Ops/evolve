"""bot_version_sync.py — version stamping + identity-based sync detection.

Extracted from the size-capped ``deploy.py`` (a frozen hot-hazard file). These
are pure, dependency-light helpers — they take the repo root / current identity
as arguments and never import ``deploy`` — so the cycle stays one-way
(``deploy`` imports this; this imports nothing of ours). ``deploy.py`` keeps the
thin wrappers that bind them to the running checkout's module globals
(``EVOLVE_VERSION`` / ``EVOLVE_COMMIT_SHA`` / ``EVOLVE_COMMIT_COUNT``).

The reason this code exists as its own unit: the human-readable version string
``YYYY.MMDD.<PR#>`` is NOT monotonic (a PR number is assigned at PR creation, so
a lower-numbered PR can squash-merge after a higher one — the 2026-06-25
incident, tip #3272 → #3269). So the synced / outdated DECISION is based on
commit identity (sha + ``git rev-list --count HEAD``, which strictly increases
along the ff-only deploy history), while the version string is display-only.
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from typing import Any


def compute_version(repo_root: Any) -> str:
    """Human-readable version from the latest commit: ``YYYY.MMDD.PR``.

    DISPLAY string only — operators read "v2026.0515.1173". NOT monotonic (see
    the module docstring); never lexically compare it to decide synced/outdated.
    Falls back to ``YYYY.MMDD.0`` when no PR number is in the subject (direct
    push), and to a date-only dev string when git is unavailable.
    """
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%cd %s", "--date=format:%Y.%m%d"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=5,
        )
        line = result.stdout.strip()
        if not line or result.returncode != 0:
            raise RuntimeError("git log returned nothing")
        date_part = line[:9]  # "2026.0515"
        m = re.search(r'\(#(\d+)\)\s*$', line)
        pr = m.group(1) if m else "0"
        return f"{date_part}.{pr}"
    except Exception:
        from datetime import date
        return f"{date.today().strftime('%Y.%m%d')}.0"


def compute_commit_identity(repo_root: Any, log: Any = None) -> tuple[str, int | None]:
    """Return ``(head_sha, commit_count)`` — the MONOTONIC identity behind the
    synced/outdated decision.

    ``head_sha`` is the exact commit ("is the bot on the same commit I am?").
    ``commit_count`` (``git rev-list --count HEAD``) strictly *increases* on
    every ``git pull --ff-only`` — the deploy checkout's only advance — so
    comparing counts can never report "current is a LOWER number than deployed"
    for a bot that is genuinely behind. Returns ``("", None)`` when git is
    unavailable; each component degrades independently. Failures are logged via
    ``log`` (a logger), not swallowed.
    """
    sha = ""
    count: int | None = None
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            sha = r.stdout.strip()
    except Exception as e:
        if log is not None:
            log.debug("commit-identity: rev-parse HEAD failed: %s", e)
    try:
        # `--count` and `--format` are mutually exclusive (`--count` wins and
        # suppresses the per-commit format), so count is its own call.
        r = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip().isdigit():
            count = int(r.stdout.strip())
    except Exception as e:
        if log is not None:
            log.debug("commit-identity: rev-list --count HEAD failed: %s", e)
    return sha, count


def build_deploy_stamp(
    version: str, sha: str, commit_count: int | None,
    deployed_at: str | None = None,
) -> dict[str, Any]:
    """The ``install.json::bot_versions[bot_id]`` record for the current code.

    Carries ``version`` (display, NOT monotonic), ``deployed_at`` (ISO, defaults
    to now), and — when git resolved them — ``sha`` / ``commit_count`` (the
    identity :func:`classify_sync` decides on). The identity fields are omitted
    when absent, preserving the pre-identity stamp shape for back-compat.
    """
    rec: dict[str, Any] = {
        "version": version,
        "deployed_at": deployed_at or datetime.now(timezone.utc).isoformat(),
    }
    if sha:
        rec["sha"] = sha
    if commit_count is not None:
        rec["commit_count"] = commit_count
    return rec


def classify_sync(
    deployed_version: str | None,
    deployed_sha: str | None,
    deployed_count: int | None,
    current_sha: str,
    current_count: int | None,
    current_version: str,
) -> tuple[bool, str]:
    """Decide synced vs outdated by MONOTONIC commit identity, not the version
    string. Returns ``(synced, relation)`` with ``relation`` one of
    ``never`` / ``synced`` / ``behind`` / ``ahead`` / ``unknown``.

    Comparison is by ``commit_count`` (or sha equality), never by lexically
    comparing PR numbers — so a bot genuinely behind can never be told "current
    is a LOWER number than you have" (the 2026-06-25 bug).
    """
    # Never deployed — no version and no sha.
    if not deployed_version and not deployed_sha:
        return False, "never"
    # Identity path: both sides carry a sha. Exact match → synced.
    if deployed_sha and current_sha:
        if deployed_sha == current_sha:
            return True, "synced"
        # Different commits — order by the monotonic count, never by PR#.
        if isinstance(deployed_count, int) and isinstance(current_count, int):
            if deployed_count < current_count:
                return False, "behind"
            if deployed_count > current_count:
                return False, "ahead"
            # Equal count, different sha → divergent history; can't claim behind.
            return False, "unknown"
        return False, "unknown"
    # Legacy stamp (predates the sha field) or git unavailable on the admin
    # server: fall back to version-string equality for the synced decision, but
    # a mismatch is "unknown" (NOT "behind") — without identity we must never
    # render the harsh "outdated → a lower number" affordance.
    if deployed_version and deployed_version == current_version:
        return True, "synced"
    return False, "unknown"


def build_sync_status(
    members: list[str],
    bot_versions: dict[str, dict],
    current_version: str,
    current_sha: str,
    current_count: int | None,
) -> dict[str, dict]:
    """Per-bot sync state keyed by bot_id, decided by :func:`classify_sync`.

    Each value carries the display ``deployed_version`` / ``current_version``
    plus the identity-derived ``synced`` flag and ``relation`` (and the sha
    pair), so no surface can invert "outdated" when a later-merged PR has a
    lower number. A bot with no entry is never-deployed (synced=False).
    """
    result: dict[str, dict] = {}
    for bot_id in members:
        bv = bot_versions.get(bot_id, {})
        deployed = bv.get("version")
        dep_sha = bv.get("sha")
        dep_count = bv.get("commit_count")
        synced, relation = classify_sync(
            deployed, dep_sha, dep_count, current_sha, current_count,
            current_version,
        )
        result[bot_id] = {
            "deployed_version": deployed,
            "deployed_at": bv.get("deployed_at"),
            "deployed_sha": dep_sha,
            "synced": synced,
            "relation": relation,
            "current_version": current_version,
            "current_sha": current_sha or None,
        }
    return result
