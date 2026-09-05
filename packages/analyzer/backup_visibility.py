"""backup_visibility — check whether a GitHub backup repo is private.

Backup repos are operator-supplied and operator-created (Evolve does not
run ``gh repo create``). GitHub defaults new repos to public; a single
misclick at create time, or someone flipping visibility through the
GitHub UI later, would expose the entire cloud-eligible workspace.

This module is the single source of truth for "is the repo private?"
It's called from three places:

  1. ``/api/backup/cloud/config`` PATCH — when the operator sets a
     ``backupRepoUrl``, refuse if the repo isn't private.
  2. ``/api/backup/cloud/init`` — second-chance check before any
     workspace git state gets initialized.
  3. ``backup.py`` push path — final guard, runs every backup. Refuses
     to push if the remote isn't private.

A periodic monitor (in ``backup_signal``) calls this on a cadence so an
out-of-band visibility flip surfaces in the admin UI within an hour.

Spec: internal/spec-backup-and-data-classification-2026-05-28.md §"Phase 1
— Public-repo guard".
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Literal

from evolve_config import load_config

Visibility = Literal["private", "public", "unknown"]


_API_TIMEOUT_S = 5.0
_GITHUB_API = "https://api.github.com"

# git@github.com:owner/name(.git)? OR https://github.com/owner/name(.git)?
_SSH_URL_RE = re.compile(
    r"^git@github\.com:(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$"
)
_HTTPS_URL_RE = re.compile(
    r"^https?://(?:[^@/]+@)?github\.com/(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$"
)


def parse_github_repo(repo_url: str) -> tuple[str, str] | None:
    """Extract (owner, name) from a GitHub URL. Returns None if it doesn't parse."""
    if not repo_url:
        return None
    s = repo_url.strip()
    m = _SSH_URL_RE.match(s) or _HTTPS_URL_RE.match(s)
    if not m:
        return None
    return m.group("owner"), m.group("name")


def _keystore_pat(config: dict) -> str | None:
    """Read the PAT from the keystore (its canonical home since 2.8).

    The vault accessors live in ``evolve_admin.keystore`` — importable
    directly since Phase 6.1's editable installs (same best-effort
    pattern as heal.py's catalog import). Any failure (admin package
    unimportable, no keystore entry) returns None and the caller falls
    back to the legacy network.json location.
    """
    try:
        from evolve_admin.keystore import (  # pyright: ignore[reportMissingImports]
            load_github_pat,
        )
        shared = Path(config.get("sharedDir") or "/Users/Shared/evolve")
        return load_github_pat(shared)
    except Exception:
        return None


def load_pat(config: dict | None = None) -> str | None:
    """Read the pod-wide GitHub PAT.

    Keystore-first (roadmap 2.8 — the PAT no longer persists in
    network.json), with a legacy fallback to ``network.json::github.pat``
    for pods the startup migration hasn't reached yet.

    Empty string and missing both return None — the caller treats that as
    "no PAT, can't verify, fail-safe to unknown".
    """
    if config is None:
        config = load_config()
    pat = _keystore_pat(config)
    if pat:
        return pat
    github = config.get("github") if isinstance(config.get("github"), dict) else {}
    pat = (github.get("pat") or "").strip() if isinstance(github, dict) else ""
    return pat or None


def check_repo_visibility(
    repo_url: str,
    *,
    pat: str | None = None,
    config: dict | None = None,
    _opener=None,
) -> Visibility:
    """Return the GitHub visibility of ``repo_url``.

    ``"private"`` → repo confirmed private; push is safe.
    ``"public"``  → repo confirmed public; push must be refused.
    ``"unknown"`` → couldn't determine (no PAT, network error, 4xx, malformed URL).

    Callers treat ``unknown`` the same as ``public`` for guard purposes —
    we'd rather miss a backup than leak a workspace. The Signal copy for
    ``unknown`` vs ``public`` should differ ("configure your PAT" vs
    "your repo is public"), so callers can branch on the return value.

    ``_opener`` is a test hook: pass a callable that mimics
    ``urllib.request.urlopen`` to inject responses without touching the
    network.
    """
    parsed = parse_github_repo(repo_url)
    if parsed is None:
        return "unknown"
    owner, name = parsed

    if pat is None:
        pat = load_pat(config)
    if not pat:
        return "unknown"

    req = urllib.request.Request(
        f"{_GITHUB_API}/repos/{owner}/{name}",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "evolve-backup-visibility",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    opener = _opener or urllib.request.urlopen
    try:
        with opener(req, timeout=_API_TIMEOUT_S) as resp:
            if resp.status != 200:
                return "unknown"
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return "unknown"

    # GitHub returns both ``private: bool`` and ``visibility: "public"|"private"|"internal"``.
    # ``internal`` only exists on Enterprise; treat as private (org-restricted).
    visibility = payload.get("visibility")
    if visibility == "public":
        return "public"
    if visibility in ("private", "internal"):
        return "private"
    # Fall back to the boolean if ``visibility`` is missing (older responses).
    if payload.get("private") is True:
        return "private"
    if payload.get("private") is False:
        return "public"
    return "unknown"
