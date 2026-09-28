"""F-P.12.d — contributor identity normalizer tests.

The contributor field is optional metadata identifying who shared a
community-sourced gallery package. The install confirmation gate
surfaces it so operators can decide based on the source, not just
the trust tier.

These tests cover the normalize_contributor helper that defends
against malformed / partial contributor blocks landing in the
UI without crashing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.gallery import (  # noqa: E402
    list_gallery_packages,
    normalize_contributor,
)


# ── normalize_contributor — happy path ──────────────────────────────────────


def test_full_contributor_block_round_trips():
    raw = {
        "name": "Alex Developer",
        "handle": "alex_dev",
        "url": "https://example.com/alex",
    }
    assert normalize_contributor(raw) == raw


def test_name_only_block_is_valid():
    assert normalize_contributor({"name": "Robin"}) == {"name": "Robin"}


def test_name_plus_handle_only():
    assert normalize_contributor({"name": "Alex", "handle": "alex"}) == {
        "name": "Alex", "handle": "alex",
    }


def test_name_plus_url_only():
    assert normalize_contributor({
        "name": "Alex", "url": "https://example.com",
    }) == {"name": "Alex", "url": "https://example.com"}


# ── normalize_contributor — defensive cases ─────────────────────────────────


def test_missing_name_returns_empty():
    """The strict ``name``-or-nothing rule prevents an "anonymous
    unknown" half-state in the install gate."""
    assert normalize_contributor({"handle": "alex_dev"}) == {}
    assert normalize_contributor({"url": "https://example.com"}) == {}
    assert normalize_contributor({}) == {}


def test_empty_name_returns_empty():
    assert normalize_contributor({"name": ""}) == {}
    assert normalize_contributor({"name": "   "}) == {}


def test_non_string_name_returns_empty():
    """Defensive: if the JSON has `name: 42` the loader doesn't
    surface that as a contributor at all."""
    assert normalize_contributor({"name": 42}) == {}
    assert normalize_contributor({"name": None}) == {}
    assert normalize_contributor({"name": ["Alex"]}) == {}


def test_non_dict_raw_returns_empty():
    assert normalize_contributor(None) == {}
    assert normalize_contributor("Alex") == {}
    assert normalize_contributor(42) == {}
    assert normalize_contributor(["Alex"]) == {}


def test_whitespace_in_fields_gets_stripped():
    assert normalize_contributor({
        "name": "  Alex  ",
        "handle": "  alex_dev  ",
        "url": "  https://example.com  ",
    }) == {
        "name": "Alex", "handle": "alex_dev", "url": "https://example.com",
    }


def test_empty_handle_dropped_not_in_output():
    assert normalize_contributor({"name": "Alex", "handle": ""}) == {"name": "Alex"}
    assert normalize_contributor({"name": "Alex", "url": "   "}) == {"name": "Alex"}


def test_unknown_keys_dropped():
    """Forward-compat: an unknown key in the contributor block
    doesn't leak through. If new fields land later (e.g.
    public_key for verifying signatures in F-P.13), the loader
    will be updated to recognize them explicitly."""
    out = normalize_contributor({
        "name": "Alex",
        "secret_token": "xyz",          # not recognized
        "internal_id": 42,              # not recognized
    })
    assert out == {"name": "Alex"}
    assert "secret_token" not in out


# ── list_gallery_packages integration ──────────────────────────────────────


@pytest.fixture
def shared_with_community_pkg(tmp_path: Path):
    """An imported package with a contributor block."""
    imported = tmp_path / "gallery" / "imported"
    imported.mkdir(parents=True)
    (imported / "p-11111111.json").write_text(json.dumps({
        "pkg_id": "p-11111111",
        "name": "community-pkg",
        "display_name": "Community Pkg",
        "objective": "test",
        "build_spec": "...",
        "contributor": {
            "name": "Alex Developer",
            "handle": "alex_dev",
            "url": "https://github.com/alex_dev",
        },
    }))
    (imported / "p-22222222.json").write_text(json.dumps({
        "pkg_id": "p-22222222",
        "name": "no-contributor",
        "display_name": "No Contributor",
        "objective": "test",
        "build_spec": "...",
    }))
    (imported / "p-33333333.json").write_text(json.dumps({
        "pkg_id": "p-33333333",
        "name": "malformed-contributor",
        "display_name": "Malformed",
        "objective": "test",
        "build_spec": "...",
        "contributor": {"handle": "alex"},  # missing name
    }))
    return tmp_path


def test_list_packages_surfaces_contributor(shared_with_community_pkg):
    pkgs = list_gallery_packages(shared_with_community_pkg, bot_ids=[])
    by_id = {p["pkg_id"]: p for p in pkgs}
    target = by_id["p-11111111"]
    assert target["contributor"]["name"] == "Alex Developer"
    assert target["contributor"]["handle"] == "alex_dev"
    assert target["contributor"]["url"] == "https://github.com/alex_dev"


def test_list_packages_empty_contributor_when_absent(shared_with_community_pkg):
    pkgs = list_gallery_packages(shared_with_community_pkg, bot_ids=[])
    by_id = {p["pkg_id"]: p for p in pkgs}
    assert by_id["p-22222222"]["contributor"] == {}


def test_list_packages_malformed_contributor_normalized_to_empty(
    shared_with_community_pkg,
):
    """Malformed contributor (missing name) gets normalized to {} —
    the install gate then doesn't try to render "Contributed by
    None" garbage."""
    pkgs = list_gallery_packages(shared_with_community_pkg, bot_ids=[])
    by_id = {p["pkg_id"]: p for p in pkgs}
    assert by_id["p-33333333"]["contributor"] == {}
