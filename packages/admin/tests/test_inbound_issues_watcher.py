"""Tests for the inbound_issues_watcher (Phase 4a).

Strategy: stub ALL the network seams — _gh_self_login, _gh_list_recent_issues,
_is_maintainer_on, the keystore, and the triager. Exercises the
orchestrator's logic in isolation:

  - Skips non-maintainer repos
  - Skips operator's own issues (self-authored)
  - Skips already-captured issues (idempotency)
  - Captures new issues as inbound intakes with triage records
  - Handles auth failures gracefully (logs + counts, doesn't crash)
  - Feature-gate respected (no-op when disabled)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
_ANALYZER_PKG = _ADMIN_PKG.parent / "analyzer"
for p in (str(_ADMIN_PKG), str(_ANALYZER_PKG)):
    if p not in sys.path:
        sys.path.insert(0, p)

import inbound_issues_watcher as iiw  # noqa: E402
from evolve_admin.intake import classifier as cls  # noqa: E402
from evolve_admin.intake import store as _store  # noqa: E402


@pytest.fixture(autouse=True)
def reset_triager():
    yield
    cls.set_triager(None)


def _network(tmp_path):
    return {
        "sharedDir": str(tmp_path),
        "intake": {
            "github": {
                "default": "evolve",
                "targets": {
                    "evolve": {
                        "owner": "evolve-ops", "repo": "evolve",
                        "token_slot": "github_intake",
                    },
                },
            },
        },
    }


def _install_json_enabled(tmp_path):
    p = tmp_path / "install.json"
    p.write_text(json.dumps({
        "feature_profile": "developer",
        "features": {"inbound_issues_watcher": {"enabled": True}},
    }))
    return p


def _install_json_disabled(tmp_path):
    p = tmp_path / "install.json"
    p.write_text(json.dumps({"feature_profile": "standard"}))
    return p


def _stub_keystore(token_map: dict[str, str]):
    """Build a KeystoreManager stub that returns a token per slot."""
    class _Stub:
        def __init__(self, *a, **kw):
            pass
        def get_value(self, slot):
            return token_map.get(slot)
    return _Stub


def _stub_triager_route(category="bug", urgency="p2", confidence=0.85):
    cls.set_triager(lambda t, b, r, a, c: cls.TriageVerdict(
        category=category, urgency=urgency,
        recommendation="route_to_admin",
        confidence=confidence, reasoning="stub",
    ))


# ─── Feature gating ───────────────────────────────────────────────────────


def test_disabled_feature_is_noop(tmp_path):
    """install_profile says off → orchestrator exits early."""
    ij = _install_json_disabled(tmp_path)
    stats = iiw.run_once(
        shared_dir=tmp_path, network=_network(tmp_path), install_json=ij,
    )
    assert stats == {
        "repos_polled": 0, "captured": 0, "skipped": 0,
        "auth_failures": 0,
    }


# ─── Maintainer-tier gating ───────────────────────────────────────────────


def test_non_maintainer_repo_skipped(tmp_path, monkeypatch):
    """If gh says the operator has only 'read' on a configured repo,
    the watcher must skip it — nothing actionable to triage anyway."""
    ij = _install_json_enabled(tmp_path)
    _stub_triager_route()
    monkeypatch.setattr(iiw, "_gh_self_login", lambda: "cjalden")
    monkeypatch.setattr(iiw, "_is_maintainer_on",
                        lambda owner, repo, login, token: False)
    monkeypatch.setattr("evolve_admin.keystore.KeystoreManager",
                        _stub_keystore({"github_intake": "tkn"}))

    stats = iiw.run_once(
        shared_dir=tmp_path, network=_network(tmp_path), install_json=ij,
    )
    assert stats["repos_polled"] == 0
    assert stats["captured"] == 0


# ─── Self-authored filter ─────────────────────────────────────────────────


def test_skips_operator_authored_issues(tmp_path, monkeypatch):
    """Issues filed by the operator themselves belong to
    upstream_issues_watcher's surface, not this one."""
    ij = _install_json_enabled(tmp_path)
    _stub_triager_route()
    monkeypatch.setattr(iiw, "_gh_self_login", lambda: "cjalden")
    monkeypatch.setattr(iiw, "_is_maintainer_on",
                        lambda *a, **kw: True)
    monkeypatch.setattr("evolve_admin.keystore.KeystoreManager",
                        _stub_keystore({"github_intake": "tkn"}))
    monkeypatch.setattr(iiw, "_gh_list_recent_issues",
                        lambda repo, since_iso=None: [
                            {
                                "number": 1, "title": "self-authored",
                                "author": {"login": "cjalden"},  # self!
                                "body": "x", "state": "OPEN",
                                "createdAt": "2026-05-23T00:00:00Z",
                                "url": "https://github.com/evolve-ops/evolve/issues/1",
                            },
                        ])

    stats = iiw.run_once(
        shared_dir=tmp_path, network=_network(tmp_path), install_json=ij,
    )
    assert stats["captured"] == 0
    assert stats["skipped"] == 1


# ─── Happy path: capture a new inbound issue ──────────────────────────────


def test_captures_new_inbound_issue_with_triage(tmp_path, monkeypatch):
    ij = _install_json_enabled(tmp_path)
    _stub_triager_route(category="bug", urgency="p1", confidence=0.9)
    monkeypatch.setattr(iiw, "_gh_self_login", lambda: "cjalden")
    monkeypatch.setattr(iiw, "_is_maintainer_on", lambda *a, **kw: True)
    monkeypatch.setattr("evolve_admin.keystore.KeystoreManager",
                        _stub_keystore({"github_intake": "tkn"}))
    monkeypatch.setattr(iiw, "_gh_list_recent_issues",
                        lambda repo, since_iso=None: [
                            {
                                "number": 42, "title": "Gateway crash",
                                "author": {"login": "someone-else"},
                                "body": "Repro: do X, see crash.",
                                "state": "OPEN",
                                "createdAt": "2026-05-23T10:00:00Z",
                                "url": "https://github.com/evolve-ops/evolve/issues/42",
                            },
                        ])

    stats = iiw.run_once(
        shared_dir=tmp_path, network=_network(tmp_path), install_json=ij,
    )
    assert stats["captured"] == 1
    assert stats["repos_polled"] == 1

    # Intake should be on disk in state=filed, inbound=True.
    found = _store.find_by_github_issue(tmp_path, "evolve-ops/evolve", 42)
    assert found is not None
    assert found.inbound is True
    assert found.state == "filed"
    assert found.triage is not None
    assert found.triage.category == "bug"
    assert found.triage.urgency == "p1"
    assert found.triage.inbound_author == "someone-else"
    assert found.triage.inbound_title == "Gateway crash"
    assert "Repro" in found.triage.inbound_body_short
    # Promotion fields populated.
    assert found.promotion.github_issue_url.endswith("/issues/42")
    assert found.promotion.github_issue_number == 42


# ─── Idempotency ──────────────────────────────────────────────────────────


def test_already_captured_issue_skipped(tmp_path, monkeypatch):
    """Second run sees the same issue → idempotent skip, not re-capture."""
    ij = _install_json_enabled(tmp_path)
    _stub_triager_route()
    monkeypatch.setattr(iiw, "_gh_self_login", lambda: "cjalden")
    monkeypatch.setattr(iiw, "_is_maintainer_on", lambda *a, **kw: True)
    monkeypatch.setattr("evolve_admin.keystore.KeystoreManager",
                        _stub_keystore({"github_intake": "tkn"}))
    monkeypatch.setattr(iiw, "_gh_list_recent_issues",
                        lambda repo, since_iso=None: [
                            {
                                "number": 99, "title": "X",
                                "author": {"login": "user1"},
                                "body": "X", "state": "OPEN",
                                "createdAt": "2026-05-23T10:00:00Z",
                                "url": "https://github.com/evolve-ops/evolve/issues/99",
                            },
                        ])

    s1 = iiw.run_once(
        shared_dir=tmp_path, network=_network(tmp_path), install_json=ij,
    )
    assert s1["captured"] == 1

    s2 = iiw.run_once(
        shared_dir=tmp_path, network=_network(tmp_path), install_json=ij,
    )
    assert s2["captured"] == 0
    assert s2["skipped"] == 1


# ─── Auth failure ─────────────────────────────────────────────────────────


def test_auth_failure_on_self_login_returns_noop(tmp_path, monkeypatch):
    """gh auth blowing up on the first call (resolving identity) should
    NOT crash — counted as an auth_failure, run returns the empty stats."""
    ij = _install_json_enabled(tmp_path)
    def boom():
        raise iiw._GhAuthError("not authenticated")
    monkeypatch.setattr(iiw, "_gh_self_login", boom)

    stats = iiw.run_once(
        shared_dir=tmp_path, network=_network(tmp_path), install_json=ij,
    )
    assert stats["auth_failures"] >= 1
    assert stats["captured"] == 0


def test_auth_failure_on_one_repo_skips_it(tmp_path, monkeypatch):
    """Per-repo gh auth failure during the issue list → skip that repo,
    keep going for any others. Single-repo case still records the
    auth-failure count."""
    ij = _install_json_enabled(tmp_path)
    _stub_triager_route()
    monkeypatch.setattr(iiw, "_gh_self_login", lambda: "cjalden")
    monkeypatch.setattr(iiw, "_is_maintainer_on", lambda *a, **kw: True)
    monkeypatch.setattr("evolve_admin.keystore.KeystoreManager",
                        _stub_keystore({"github_intake": "tkn"}))
    def boom(repo, since_iso=None):
        raise iiw._GhAuthError("token expired")
    monkeypatch.setattr(iiw, "_gh_list_recent_issues", boom)

    stats = iiw.run_once(
        shared_dir=tmp_path, network=_network(tmp_path), install_json=ij,
    )
    assert stats["repos_polled"] == 1
    assert stats["auth_failures"] >= 1
    assert stats["captured"] == 0


# ─── Dry-run ──────────────────────────────────────────────────────────────


def test_dry_run_does_not_capture(tmp_path, monkeypatch):
    ij = _install_json_enabled(tmp_path)
    _stub_triager_route()
    monkeypatch.setattr(iiw, "_gh_self_login", lambda: "cjalden")
    monkeypatch.setattr(iiw, "_is_maintainer_on", lambda *a, **kw: True)
    monkeypatch.setattr("evolve_admin.keystore.KeystoreManager",
                        _stub_keystore({"github_intake": "tkn"}))
    monkeypatch.setattr(iiw, "_gh_list_recent_issues",
                        lambda repo, since_iso=None: [
                            {
                                "number": 1, "title": "X",
                                "author": {"login": "user1"},
                                "body": "x", "state": "OPEN",
                                "createdAt": "z", "url": "u",
                            },
                        ])

    stats = iiw.run_once(
        shared_dir=tmp_path, network=_network(tmp_path), install_json=ij,
        dry_run=True,
    )
    assert stats["captured"] == 0
    assert stats["skipped"] == 1
    # No intake on disk.
    assert _store.find_by_github_issue(tmp_path, "evolve-ops/evolve", 1) is None
