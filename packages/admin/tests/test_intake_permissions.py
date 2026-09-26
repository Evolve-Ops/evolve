"""Tests for ``intake.permissions`` (Phase 3 of Issue Inbox).

Strategy: substitute the transport seam with a deterministic stub that
maps URL → (status, response). Verifies caching, all permission tiers,
graceful degradation on token problems / network errors / unknown
permission strings.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

from evolve_admin.intake import permissions as perms  # noqa: E402


@pytest.fixture(autouse=True)
def clear_caches():
    perms.clear_caches()
    yield
    perms.clear_caches()


def _transport(routes: dict):
    """Build a transport stub from a {url: (status, response)} map.

    Returns the recorded call count alongside the transport so tests
    can assert caching behavior (one call, not two, etc.).
    """
    calls: list[tuple[str, str]] = []

    def tx(method, url, headers, body):
        calls.append((method, url))
        if url in routes:
            return routes[url]
        return (404, {"error": "no stub for url"})

    return tx, calls


# ─── get_self_login ───────────────────────────────────────────────────────


def test_get_self_login_returns_login():
    tx, _ = _transport({
        "https://api.github.com/user": (200, {"login": "cjalden"}),
    })
    assert perms.get_self_login("tkn", transport=tx) == "cjalden"


def test_get_self_login_strips_whitespace():
    tx, _ = _transport({
        "https://api.github.com/user": (200, {"login": "  cjalden  "}),
    })
    assert perms.get_self_login("tkn", transport=tx) == "cjalden"


def test_get_self_login_none_on_missing_token():
    assert perms.get_self_login(None) is None
    assert perms.get_self_login("") is None
    assert perms.get_self_login("   ") is None


def test_get_self_login_none_on_non_200():
    tx, _ = _transport({
        "https://api.github.com/user": (401, {"message": "Bad credentials"}),
    })
    assert perms.get_self_login("tkn", transport=tx) is None


def test_get_self_login_none_on_malformed_response():
    """200 OK but the response body doesn't have a usable login →
    return None rather than guessing."""
    tx, _ = _transport({
        "https://api.github.com/user": (200, {"name": "no login field"}),
    })
    assert perms.get_self_login("tkn", transport=tx) is None


def test_get_self_login_caches_result():
    tx, calls = _transport({
        "https://api.github.com/user": (200, {"login": "cjalden"}),
    })
    perms.get_self_login("tkn", transport=tx)
    perms.get_self_login("tkn", transport=tx)
    assert len(calls) == 1, (
        f"second call should hit cache; got {len(calls)} round-trips"
    )


def test_get_self_login_cache_keyed_by_token():
    """Different tokens must not share a cache slot — even an empty
    login response on one token shouldn't poison another."""
    tx_a, _ = _transport({
        "https://api.github.com/user": (200, {"login": "alice"}),
    })
    tx_b, _ = _transport({
        "https://api.github.com/user": (200, {"login": "bob"}),
    })
    assert perms.get_self_login("token-a", transport=tx_a) == "alice"
    assert perms.get_self_login("token-b", transport=tx_b) == "bob"


# ─── get_permission ───────────────────────────────────────────────────────


def test_get_permission_admin_tier():
    tx, _ = _transport({
        "https://api.github.com/repos/x/y/collaborators/me/permission":
            (200, {"permission": "admin"}),
    })
    assert perms.get_permission(
        owner="x", repo="y", login="me", token="tkn", transport=tx,
    ) == "admin"


def test_get_permission_each_known_tier():
    """Verify all six documented tiers + the inferred 'none' from 404
    all round-trip cleanly."""
    cases = [
        ("admin", 200, {"permission": "admin"}),
        ("maintain", 200, {"permission": "maintain"}),
        ("triage", 200, {"permission": "triage"}),
        ("write", 200, {"permission": "write"}),
        ("read", 200, {"permission": "read"}),
        ("none", 200, {"permission": "none"}),
    ]
    for expected, status, body in cases:
        perms.clear_caches()
        tx, _ = _transport({
            "https://api.github.com/repos/x/y/collaborators/me/permission":
                (status, body),
        })
        assert perms.get_permission(
            owner="x", repo="y", login="me", token="tkn", transport=tx,
        ) == expected


def test_get_permission_404_means_not_a_collaborator():
    """Per GitHub's API: collaborator-check 404 means the username
    isn't a collaborator on the repo (regardless of public/private).
    Only fires when login != owner — when they match, the 404 means
    the token can't even see the repo (scope problem), not that the
    owner isn't a collaborator on their own repo."""
    tx, _ = _transport({
        "https://api.github.com/repos/x/y/collaborators/me/permission":
            (404, {"message": "Not Found"}),
    })
    assert perms.get_permission(
        owner="x", repo="y", login="me", token="tkn", transport=tx,
    ) == "none"


def test_get_permission_404_when_login_owns_repo_is_scope_mismatch():
    """When the token's self-login matches the repo owner AND the
    collaborator endpoint returns 404, the only explanation is that
    the token cannot see the repo at all — classic PAT lacking `repo`
    scope or fine-grained PAT excluding this repo. Returning "none"
    would mislead the UI into showing "not a collaborator" when the
    fix is to rotate the PAT scope, not adjust collaborator settings.
    """
    tx, _ = _transport({
        "https://api.github.com/repos/evolve-ops/evolve/collaborators/cjalden/permission":
            (404, {"message": "Not Found"}),
    })
    assert perms.get_permission(
        owner="cjalden", repo="evolve", login="cjalden",
        token="tkn", transport=tx,
    ) == "scope_mismatch"


def test_get_permission_scope_mismatch_case_insensitive_login_match():
    """GitHub logins are case-insensitive — the disambiguation must
    match @CJAlden against owner cjalden the same as @cjalden."""
    tx, _ = _transport({
        "https://api.github.com/repos/evolve-ops/evolve/collaborators/CJAlden/permission":
            (404, {"message": "Not Found"}),
    })
    assert perms.get_permission(
        owner="cjalden", repo="evolve", login="CJAlden",
        token="tkn", transport=tx,
    ) == "scope_mismatch"


def test_get_permission_unknown_on_non_2xx_non_404():
    """500/network/etc. errors should NOT be conflated with 'no perm'.
    UI treats 'unknown' as 'show no badge'; conflating with 'none'
    would hide the maintainer affordances when GitHub is just slow."""
    tx, _ = _transport({
        "https://api.github.com/repos/x/y/collaborators/me/permission":
            (500, {"error": "internal"}),
    })
    assert perms.get_permission(
        owner="x", repo="y", login="me", token="tkn", transport=tx,
    ) == "unknown"


def test_get_permission_unknown_on_unrecognized_perm_string():
    """Defensive: a future GitHub permission string we don't know about
    should NOT be auto-promoted to maintainer. Treat as unknown."""
    tx, _ = _transport({
        "https://api.github.com/repos/x/y/collaborators/me/permission":
            (200, {"permission": "future-permission"}),
    })
    assert perms.get_permission(
        owner="x", repo="y", login="me", token="tkn", transport=tx,
    ) == "unknown"


def test_get_permission_unknown_on_missing_token():
    assert perms.get_permission(
        owner="x", repo="y", login="me", token=None,
    ) == "unknown"


def test_get_permission_unknown_on_missing_login():
    assert perms.get_permission(
        owner="x", repo="y", login="", token="tkn",
    ) == "unknown"


def test_get_permission_caches_result():
    tx, calls = _transport({
        "https://api.github.com/repos/x/y/collaborators/me/permission":
            (200, {"permission": "triage"}),
    })
    perms.get_permission(owner="x", repo="y", login="me", token="t", transport=tx)
    perms.get_permission(owner="x", repo="y", login="me", token="t", transport=tx)
    assert len(calls) == 1


def test_get_permission_cache_keyed_by_login_owner_repo():
    """Different login OR different repo must NOT share a cache slot —
    swapping logins or repos must trigger a fresh lookup."""
    routes = {
        "https://api.github.com/repos/x/y/collaborators/alice/permission":
            (200, {"permission": "admin"}),
        "https://api.github.com/repos/x/y/collaborators/bob/permission":
            (200, {"permission": "read"}),
        "https://api.github.com/repos/x/z/collaborators/alice/permission":
            (200, {"permission": "none"}),
    }
    tx, calls = _transport(routes)
    assert perms.get_permission(owner="x", repo="y", login="alice",
                                token="t", transport=tx) == "admin"
    assert perms.get_permission(owner="x", repo="y", login="bob",
                                token="t", transport=tx) == "read"
    assert perms.get_permission(owner="x", repo="z", login="alice",
                                token="t", transport=tx) == "none"
    # Three distinct lookups → three calls.
    assert len(calls) == 3


def test_clear_caches_drops_both():
    tx_login, calls_login = _transport({
        "https://api.github.com/user": (200, {"login": "x"}),
    })
    tx_perm, calls_perm = _transport({
        "https://api.github.com/repos/x/y/collaborators/x/permission":
            (200, {"permission": "admin"}),
    })
    perms.get_self_login("t", transport=tx_login)
    perms.get_permission(owner="x", repo="y", login="x",
                         token="t", transport=tx_perm)
    assert len(calls_login) == 1
    assert len(calls_perm) == 1

    perms.clear_caches()
    perms.get_self_login("t", transport=tx_login)
    perms.get_permission(owner="x", repo="y", login="x",
                         token="t", transport=tx_perm)
    # Both should re-hit after clear.
    assert len(calls_login) == 2
    assert len(calls_perm) == 2


# ─── Tier helpers ─────────────────────────────────────────────────────────


def test_is_maintainer_includes_admin_maintain_triage_write():
    assert perms.is_maintainer("admin")
    assert perms.is_maintainer("maintain")
    assert perms.is_maintainer("triage")
    assert perms.is_maintainer("write")


def test_is_maintainer_excludes_read_none_unknown():
    assert not perms.is_maintainer("read")
    assert not perms.is_maintainer("none")
    assert not perms.is_maintainer("unknown")
    assert not perms.is_maintainer("scope_mismatch")
    assert not perms.is_maintainer("")


def test_maintainer_tier_label_each_tier():
    assert perms.maintainer_tier_label("admin") == "maintainer"
    assert perms.maintainer_tier_label("maintain") == "maintainer"
    assert perms.maintainer_tier_label("triage") == "triage"
    assert perms.maintainer_tier_label("write") == "triage"
    assert perms.maintainer_tier_label("read") == "read-only"
    assert perms.maintainer_tier_label("none") == "not a collaborator"
    assert perms.maintainer_tier_label("scope_mismatch") == "PAT scope too narrow"
    assert perms.maintainer_tier_label("unknown") == "unknown"
    assert perms.maintainer_tier_label("") == "unknown"
