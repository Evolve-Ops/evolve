"""GitHub permission detection for tracked intake target repos.

Phase 3 of the Issue Inbox project. Drives the "[M]" maintainer badge
on the tracked-repos UI: for each configured target, query GitHub to
find out what permission level the install's gh identity has on the
repo. That permission level then unlocks (or hides) maintainer-only
affordances — labeling, closing, triage queue access, etc. — in
downstream phases.

Uses the same urllib + Bearer-token pattern as :mod:`intake.promote`,
so we don't take a hard dep on the ``gh`` CLI being installed for the
admin process. The token comes from the same keystore slot the
PromotionTarget points at; a missing or invalid token degrades to
``permission="unknown"`` (the UI shows it as a no-op badge — the
operator should fix the keystore, but the page still loads).

Two REST calls per target on the slow path:

  GET /user                          → resolve the install's identity
  GET /repos/{owner}/{repo}/
      collaborators/{login}/permission   → that identity's perm level

We cache the self-login per token for ~5 min and the per-repo
permission per (login, repo) for ~5 min — the perm level changes
rarely, and refreshing on every page load wastes GitHub rate budget.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


GITHUB_API_BASE = "https://api.github.com"
DEFAULT_TIMEOUT_S = 10

# Cache TTL for the two lookups. Both can change but rarely (a repo
# admin removing the operator from collaborators is the most common
# transition; the operator notices via a 404 on a later post).
SELF_LOGIN_TTL_S = 5 * 60
PERMISSION_TTL_S = 5 * 60


# GitHub's permission levels, weakest → strongest. The "maintainer
# affordances" cutoff is at "triage": anyone with triage perms or
# higher can label, assign, close issues. Strictly speaking, "write"
# also includes triage; we treat both as maintainer-tier.
#
# ``scope_mismatch`` is not a GitHub-returned value — synthesized when
# the collaborators endpoint 404s for a token whose own login matches
# the repo owner. That signature only happens when a classic PAT lacks
# `repo` scope (private repos invisible) or a fine-grained PAT doesn't
# include this repo. Distinct from ``none`` ("not a collaborator") so
# the UI can guide the operator to rotate scope rather than chase the
# wrong fix.
_MAINTAINER_TIERS = frozenset({"admin", "maintain", "triage", "write"})
_KNOWN_PERMS = frozenset({"admin", "maintain", "triage", "write", "read", "none"})


# Transport seam — tests substitute a deterministic stub instead of
# hitting the network. Same shape as intake.promote.Transport.
Transport = Callable[[str, str, dict, bytes | None], tuple[int, dict]]


# ─── Caches ─────────────────────────────────────────────────────────────────


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


# Keyed by token. We don't key by anything more specific because each
# install effectively has one identity (per the v1 single-gh-token model).
_self_login_cache: dict[str, _CacheEntry] = {}

# Keyed by (login, owner, repo). Login is part of the key so a token
# rotation that changes the identity doesn't read a stale perm.
_permission_cache: dict[tuple[str, str, str], _CacheEntry] = {}


def clear_caches() -> None:
    """Drop the in-memory caches. Useful in tests + when the operator
    rotates a token (manual revalidate)."""
    _self_login_cache.clear()
    _permission_cache.clear()


# ─── Transport ──────────────────────────────────────────────────────────────


def _default_transport(
    method: str, url: str, headers: dict, body: bytes | None
) -> tuple[int, dict]:
    """Same shape as intake.promote._default_transport — lifted here to
    avoid a circular import and to keep this module standalone."""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT_S) as resp:
            status = resp.getcode() or 0
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            raw = e.read().decode("utf-8")
        except Exception:  # noqa: BLE001
            raw = ""
    except urllib.error.URLError as e:
        return 0, {"error": f"network: {e.reason}"}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": f"transport: {type(e).__name__}: {e}"}
    try:
        return status, json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return status, {"error": "non-json response", "body": raw[:500]}


def _gh_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token.strip()}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "evolve-intake-permissions",
    }


# ─── Public API ─────────────────────────────────────────────────────────────


def get_self_login(
    token: str | None,
    *,
    transport: Transport | None = None,
) -> str | None:
    """Return the GitHub login of the install's authenticated identity.

    Cached for :data:`SELF_LOGIN_TTL_S`. Returns None when:
      - token is missing or empty
      - GitHub returns non-200 (revoked token, network issue, etc.)
    """
    if not token or not token.strip():
        return None
    key = token.strip()
    now = time.monotonic()
    cached = _self_login_cache.get(key)
    if cached is not None and cached.expires_at > now:
        return cached.value

    tx = transport or _default_transport
    status, resp = tx("GET", f"{GITHUB_API_BASE}/user", _gh_headers(key), None)
    if status != 200 or not isinstance(resp, dict):
        return None
    login = resp.get("login")
    if not isinstance(login, str) or not login.strip():
        return None
    _self_login_cache[key] = _CacheEntry(
        value=login.strip(), expires_at=now + SELF_LOGIN_TTL_S,
    )
    return login.strip()


def get_permission(
    *,
    owner: str,
    repo: str,
    login: str,
    token: str | None,
    transport: Transport | None = None,
) -> str:
    """Return the permission level ``login`` has on ``owner/repo``.

    Returns one of: ``admin``, ``maintain``, ``triage``, ``write``,
    ``read``, ``none``, ``scope_mismatch``, or ``unknown`` when the
    lookup couldn't run (missing token, network failure, etc.).
    ``scope_mismatch`` is synthesized when the collaborators endpoint
    404s for a token whose login matches the repo owner — that
    signature only happens with a too-narrow token (classic PAT
    missing `repo` scope, or fine-grained PAT that excludes this
    repo); distinct from ``none`` so the UI can guide the operator
    to rotate the PAT rather than chase collaborator settings.
    The UI treats ``unknown`` as "show no badge."

    Cached per ``(login, owner, repo)`` for :data:`PERMISSION_TTL_S`.
    """
    if not token or not token.strip() or not login.strip():
        return "unknown"
    cache_key = (login.strip(), owner.strip(), repo.strip())
    now = time.monotonic()
    cached = _permission_cache.get(cache_key)
    if cached is not None and cached.expires_at > now:
        return cached.value

    tx = transport or _default_transport
    url = (
        f"{GITHUB_API_BASE}/repos/{owner.strip()}/{repo.strip()}"
        f"/collaborators/{login.strip()}/permission"
    )
    status, resp = tx("GET", url, _gh_headers(token.strip()), None)
    if status == 404:
        # GitHub returns 404 here for two distinct cases:
        #   - login is genuinely not a collaborator on owner/repo, OR
        #   - login *owns* the repo but the token can't see private
        #     repos (classic PAT missing `repo` scope, or fine-grained
        #     PAT that doesn't include this repo).
        # The login-vs-owner check disambiguates: when they match, the
        # only way to get 404 is a scope problem with the token itself.
        # Distinguishing matters because the fixes are opposite — one
        # rotates the PAT, the other adjusts repo collaborators.
        if login.strip().lower() == owner.strip().lower():
            result = "scope_mismatch"
        else:
            result = "none"
    elif status != 200 or not isinstance(resp, dict):
        result = "unknown"
    else:
        raw = resp.get("permission")
        if isinstance(raw, str) and raw in _KNOWN_PERMS:
            result = raw
        else:
            result = "unknown"
    _permission_cache[cache_key] = _CacheEntry(
        value=result, expires_at=now + PERMISSION_TTL_S,
    )
    return result


def is_maintainer(permission: str) -> bool:
    """True when the permission level unlocks maintainer affordances
    (labeling, closing, triage). Centralized here so callers don't
    re-encode the tier mapping inline."""
    return permission in _MAINTAINER_TIERS


def maintainer_tier_label(permission: str) -> str:
    """UI-friendly summary of what the permission grants. Used by the
    tracked-repos card to render a clear badge."""
    if permission in ("admin", "maintain"):
        return "maintainer"
    if permission in ("triage", "write"):
        return "triage"
    if permission == "read":
        return "read-only"
    if permission == "none":
        return "not a collaborator"
    if permission == "scope_mismatch":
        return "PAT scope too narrow"
    return "unknown"
