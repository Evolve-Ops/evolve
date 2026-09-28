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

# Tri-state for "can this process read the pod PAT?". Mirrors
# ``evolve_admin.keystore.SecretState`` as bare literals so the analyzer
# package keeps working when the admin package isn't importable.
PatState = Literal["present", "absent", "unreadable"]
PAT_PRESENT: PatState = "present"
PAT_ABSENT: PatState = "absent"
PAT_UNREADABLE: PatState = "unreadable"


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


def visibility_via_daemon(repo_url: str, *, _poster=None) -> "tuple[Visibility, str] | None":
    """Ask the admin daemon for a verdict. Returns ``(visibility, reason)``.

    Returns ``None`` — meaning "this transport is not available, try the
    direct-PAT path" — only when the socket itself cannot be reached or the
    endpoint isn't served (an admin daemon older than this change). A daemon
    that answers gets its answer honoured, including ``unknown``.

    The bot user reaches ``{shared}/admin-daemon.sock`` by group ownership on
    macOS (mode 0660, group ``staff``) and via the ``evolve-bots`` group ACE on
    Linux (``evo_socket_acl``). No PAT is read, held, or returned here.

    ``_poster`` is a test hook mimicking ``admin_client.post_json``.
    """
    poster = _poster
    if poster is None:
        try:
            from evolve_admin.evo.admin_client import (  # pyright: ignore[reportMissingImports]
                post_json,
            )
            poster = post_json
        except Exception:
            return None
    try:
        status, body = poster(
            "/api/backup-bot/visibility", {"repoUrl": repo_url}, timeout=_API_TIMEOUT_S,
        )
    except Exception:
        # Socket missing, daemon down, connection refused → fall back.
        return None
    if status == 404:
        # Daemon predates this endpoint — fall back to the direct-PAT path so a
        # partially-rolled-out fleet keeps backing up.
        return None
    if status != 200 or not isinstance(body, dict):
        # The daemon is there and said no (403 for an unrecognized peer, 5xx).
        # That is an answer, and the fail-safe answer is ``unknown``.
        return "unknown", f"daemon-status-{status}"

    verdict = body.get("visibility")
    reason = body.get("reason")
    reason = reason if isinstance(reason, str) else "no-reason-given"
    if verdict not in ("private", "public", "unknown"):
        return "unknown", "daemon-returned-malformed-verdict"

    # The daemon binds the repo it checks to the CALLER's configured
    # backupRepoUrl. If that is not the URL we are about to push to, its verdict
    # does not cover us — refuse rather than borrow an unrelated answer.
    checked = body.get("repo_url")
    if isinstance(checked, str) and checked and checked != repo_url:
        return "unknown", "daemon-checked-a-different-repo"
    return verdict, reason


def _keystore_pat_state(config: dict) -> tuple[PatState, str | None]:
    """Tri-state read of the PAT from the keystore (its canonical home since 2.8).

    The vault accessors live in ``evolve_admin.keystore`` — importable
    directly since Phase 6.1's editable installs (same best-effort
    pattern as heal.py's catalog import).

    An admin package that won't import at all is reported ``ABSENT`` (we
    learned nothing, and the legacy network.json fallback below is the
    caller's next move). A vault that WOULD answer but refuses this uid is
    reported ``UNREADABLE`` — the distinction the 2026-08-18 outage turned on.
    """
    try:
        from evolve_admin.keystore import (  # pyright: ignore[reportMissingImports]
            SecretState,
            load_github_pat_state,
        )
    except Exception:
        return PAT_ABSENT, None
    try:
        shared = Path(config.get("sharedDir") or "/Users/Shared/evolve")
        state, value = load_github_pat_state(shared)
    except Exception:
        return PAT_UNREADABLE, None
    if state == SecretState.PRESENT and value:
        return PAT_PRESENT, value
    if state == SecretState.UNREADABLE:
        return PAT_UNREADABLE, None
    return PAT_ABSENT, None


def load_pat_state(config: dict | None = None) -> tuple[PatState, str | None]:
    """Tri-state read of the pod-wide GitHub PAT — ``(state, pat)``.

    Keystore-first (roadmap 2.8 — the PAT no longer persists in
    network.json), with a legacy fallback to ``network.json::github.pat``
    for pods the startup migration hasn't reached yet.

    ``PAT_UNREADABLE`` means a PAT is stored and this process cannot decrypt
    it — the state every bot-user backup daemon has been in since #3696
    clamped ``.machine-key``. Callers must NOT render that as "not
    configured"; it is a permission fault with a different fix.
    """
    if config is None:
        config = load_config()
    state, pat = _keystore_pat_state(config)
    if state == PAT_PRESENT:
        return PAT_PRESENT, pat
    github = config.get("github") if isinstance(config.get("github"), dict) else {}
    legacy = (github.get("pat") or "").strip() if isinstance(github, dict) else ""
    if legacy:
        return PAT_PRESENT, legacy
    # No legacy value to rescue us: preserve the keystore's own verdict, so an
    # unreadable vault stays "unreadable" rather than decaying to "absent".
    return state, None


def load_pat(config: dict | None = None) -> str | None:
    """The pod-wide GitHub PAT, or None for absent OR unreadable.

    Two-state convenience wrapper kept for callers that only need the value.
    Anything that reports the outcome to an operator must use
    :func:`load_pat_state`.
    """
    return load_pat_state(config)[1]


def check_repo_visibility(
    repo_url: str,
    *,
    pat: str | None = None,
    config: dict | None = None,
    _opener=None,
    _daemon=None,
) -> Visibility:
    """Return the GitHub visibility of ``repo_url``.

    ``"private"`` → repo confirmed private; push is safe.
    ``"public"``  → repo confirmed public; push must be refused.
    ``"unknown"`` → couldn't determine (no PAT, network error, 4xx, malformed URL).

    Callers treat ``unknown`` the same as ``public`` for guard purposes —
    we'd rather miss a backup than leak a workspace. The Signal copy for
    ``unknown`` vs ``public`` should differ ("configure your PAT" vs
    "your repo is public"), so callers can branch on the return value.

    TRANSPORT SELECTION. A caller that can read the PAT calls GitHub directly —
    that is every ``evolve``-side caller (the wizard, the monitor, the admin
    daemon's own verdict route) and its behaviour is unchanged by this seam.
    Only a caller that CANNOT read the PAT — a bot-user backup daemon, since
    #3696 — asks the admin daemon for a verdict instead. Ordering it this way
    means the daemon hop is reached exactly by the processes that were
    previously dead-ending at ``unknown``, and no existing path changes shape.
    It also makes recursion structurally impossible: the verdict route passes
    ``pat=`` explicitly, which short-circuits before any daemon call.

    ``_opener`` is a test hook: pass a callable that mimics
    ``urllib.request.urlopen`` to inject responses without touching the
    network. ``_daemon`` is the matching hook for the verdict transport.
    """
    parsed = parse_github_repo(repo_url)
    if parsed is None:
        return "unknown"
    owner, name = parsed

    if pat is None:
        state, pat = load_pat_state(config)
        if state != PAT_PRESENT or not pat:
            # We cannot verify this ourselves. Ask the daemon that can.
            answer = (
                _daemon(repo_url) if _daemon is not None
                else visibility_via_daemon(repo_url)
            )
            if answer is not None:
                verdict, _reason = answer
                return verdict
            return "unknown"
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
