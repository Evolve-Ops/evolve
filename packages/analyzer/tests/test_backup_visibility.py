"""tests/test_backup_visibility.py — repo-visibility helper tests."""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import backup_visibility as bv  # noqa: E402


# ── parse_github_repo ───────────────────────────────────────────────────────

def test_parse_ssh_url():
    assert bv.parse_github_repo("git@github.com:evolve-ops/evolve-workspace.git") == (
        "cjalden", "evolve-workspace",
    )


def test_parse_ssh_url_no_suffix():
    assert bv.parse_github_repo("git@github.com:evolve-ops/evolve-workspace") == (
        "cjalden", "evolve-workspace",
    )


def test_parse_https_url():
    assert bv.parse_github_repo("https://github.com/evolve-ops/evolve-workspace.git") == (
        "cjalden", "evolve-workspace",
    )


def test_parse_https_url_no_suffix():
    assert bv.parse_github_repo("https://github.com/evolve-ops/evolve-workspace") == (
        "cjalden", "evolve-workspace",
    )


def test_parse_https_with_token():
    # https://<token>@github.com/owner/name.git — sometimes pasted from gh helpers
    assert bv.parse_github_repo("https://ghp_xxx@github.com/evolve-ops/repo.git") == (
        "cjalden", "repo",
    )


def test_parse_trailing_slash():
    assert bv.parse_github_repo("https://github.com/evolve-ops/repo/") == ("cjalden", "repo")


def test_parse_malformed_returns_none():
    assert bv.parse_github_repo("") is None
    assert bv.parse_github_repo("not-a-url") is None
    assert bv.parse_github_repo("git@gitlab.com:owner/name.git") is None
    assert bv.parse_github_repo("https://example.com/owner/name") is None


# ── load_pat ────────────────────────────────────────────────────────────────

# Every config pins sharedDir at an empty tmp dir so the keystore-first
# path (2.8) resolves against a guaranteed-empty vault — NOT the host's
# real pod keystore at /Users/Shared/evolve (which holds a live PAT on
# deployed machines and would both fail these asserts and print it).

def test_load_pat_present(tmp_path):
    cfg = {"sharedDir": str(tmp_path), "github": {"pat": "ghp_secrettoken"}}
    assert bv.load_pat(cfg) == "ghp_secrettoken"


def test_load_pat_empty_string(tmp_path):
    assert bv.load_pat({"sharedDir": str(tmp_path), "github": {"pat": ""}}) is None


def test_load_pat_missing_block(tmp_path):
    assert bv.load_pat({"sharedDir": str(tmp_path)}) is None


def test_load_pat_strips_whitespace(tmp_path):
    cfg = {"sharedDir": str(tmp_path), "github": {"pat": "  ghp_xx  "}}
    assert bv.load_pat(cfg) == "ghp_xx"


def test_load_pat_keystore_wins_over_legacy_slot(tmp_path):
    import sys as _sys
    _admin = Path(__file__).resolve().parent.parent.parent / "admin"
    if str(_admin) not in _sys.path:
        _sys.path.insert(0, str(_admin))
    from evolve_admin.keystore import store_github_pat
    store_github_pat(tmp_path, "ghp_vault")
    cfg = {"sharedDir": str(tmp_path), "github": {"pat": "ghp_stale_legacy"}}
    assert bv.load_pat(cfg) == "ghp_vault"


# ── check_repo_visibility ───────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, status: int, body: dict | str):
        self.status = status
        if isinstance(body, dict):
            self._body = json.dumps(body).encode("utf-8")
        else:
            self._body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _opener_returning(payload: dict, status: int = 200):
    def _opener(req, timeout):
        return _FakeResponse(status, payload)
    return _opener


def _opener_raising(exc: Exception):
    def _opener(req, timeout):
        raise exc
    return _opener


def test_visibility_private_via_visibility_field():
    assert bv.check_repo_visibility(
        "git@github.com:cjalden/repo.git",
        pat="ghp_x",
        _opener=_opener_returning({"visibility": "private", "private": True}),
    ) == "private"


def test_visibility_public_via_visibility_field():
    assert bv.check_repo_visibility(
        "git@github.com:cjalden/repo.git",
        pat="ghp_x",
        _opener=_opener_returning({"visibility": "public", "private": False}),
    ) == "public"


def test_visibility_internal_treated_as_private():
    # GitHub Enterprise "internal" repos are org-restricted; safe to push to.
    assert bv.check_repo_visibility(
        "git@github.com:cjalden/repo.git",
        pat="ghp_x",
        _opener=_opener_returning({"visibility": "internal"}),
    ) == "private"


def test_visibility_falls_back_to_private_bool_when_visibility_missing():
    assert bv.check_repo_visibility(
        "git@github.com:cjalden/repo.git",
        pat="ghp_x",
        _opener=_opener_returning({"private": True}),
    ) == "private"
    assert bv.check_repo_visibility(
        "git@github.com:cjalden/repo.git",
        pat="ghp_x",
        _opener=_opener_returning({"private": False}),
    ) == "public"


def test_visibility_missing_pat_returns_unknown():
    # No PAT → can't ask GitHub → fail-safe to unknown (caller treats as public).
    assert bv.check_repo_visibility(
        "git@github.com:cjalden/repo.git",
        config={"github": {}},
    ) == "unknown"


def test_visibility_malformed_url_returns_unknown():
    assert bv.check_repo_visibility("not-a-url", pat="ghp_x") == "unknown"
    assert bv.check_repo_visibility("", pat="ghp_x") == "unknown"


def test_visibility_404_returns_unknown():
    # Repo deleted, renamed, or PAT can't see it. Don't claim "public".
    err = urllib.error.HTTPError("url", 404, "Not Found", {}, io.BytesIO(b"{}"))
    assert bv.check_repo_visibility(
        "git@github.com:cjalden/repo.git",
        pat="ghp_x",
        _opener=_opener_raising(err),
    ) == "unknown"


def test_visibility_401_returns_unknown():
    err = urllib.error.HTTPError("url", 401, "Unauthorized", {}, io.BytesIO(b"{}"))
    assert bv.check_repo_visibility(
        "git@github.com:cjalden/repo.git",
        pat="ghp_x",
        _opener=_opener_raising(err),
    ) == "unknown"


def test_visibility_network_error_returns_unknown():
    assert bv.check_repo_visibility(
        "git@github.com:cjalden/repo.git",
        pat="ghp_x",
        _opener=_opener_raising(urllib.error.URLError("connection refused")),
    ) == "unknown"


def test_visibility_timeout_returns_unknown():
    assert bv.check_repo_visibility(
        "git@github.com:cjalden/repo.git",
        pat="ghp_x",
        _opener=_opener_raising(TimeoutError("read timed out")),
    ) == "unknown"


def test_visibility_malformed_json_returns_unknown():
    assert bv.check_repo_visibility(
        "git@github.com:cjalden/repo.git",
        pat="ghp_x",
        _opener=_opener_returning("not json at all"),
    ) == "unknown"


def test_visibility_non_200_returns_unknown():
    # 200 is the only success status we trust.
    assert bv.check_repo_visibility(
        "git@github.com:cjalden/repo.git",
        pat="ghp_x",
        _opener=_opener_returning({"visibility": "private"}, status=204),
    ) == "unknown"


def test_visibility_unrecognized_payload_returns_unknown():
    # Defensive: GitHub adds a new visibility tier we don't know about.
    assert bv.check_repo_visibility(
        "git@github.com:cjalden/repo.git",
        pat="ghp_x",
        _opener=_opener_returning({"visibility": "moonshot"}),
    ) == "unknown"
