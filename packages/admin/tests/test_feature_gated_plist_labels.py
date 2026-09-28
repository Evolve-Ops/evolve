"""Tests for the feature-gated-plist catalog + ``is_label_feature_gated_off``.

Pins the contract that lets ``health._check_launchd`` distinguish
"intentionally absent because the feature is off" from "broken install" for
plists whose install path is gated by ``install_profile.is_feature_enabled``.
Without this, every standard-profile install reports a spurious "Plist not
found: …upstream-issues-watcher.plist" on every Pod Health scan even though
the absence is by design (running ``install-infra-jobs`` would be a no-op
because the gate is off).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import deploy  # noqa: E402


def _write_install_json(tmp_path: Path, payload: dict) -> Path:
    shared = tmp_path / "shared"
    shared.mkdir()
    path = shared / "install.json"
    path.write_text(json.dumps(payload))
    return path


def test_catalog_contains_both_known_watchers():
    """If a new feature-gated plist ships, add it here too. Both watchers
    live in ``expected_plist_labels`` regardless of install profile."""
    assert "ai.evolve.evolve.upstream-issues-watcher" in deploy.FEATURE_GATED_PLIST_LABELS
    assert "ai.evolve.evolve.inbound-issues-watcher" in deploy.FEATURE_GATED_PLIST_LABELS
    assert deploy.FEATURE_GATED_PLIST_LABELS[
        "ai.evolve.evolve.upstream-issues-watcher"
    ] == "upstream_issues_watcher"
    assert deploy.FEATURE_GATED_PLIST_LABELS[
        "ai.evolve.evolve.inbound-issues-watcher"
    ] == "inbound_issues_watcher"


def test_catalog_labels_are_in_expected_plist_labels():
    """A gated label must also be in ``expected_plist_labels`` — that's
    what protects it from the orphan-sweeper when the feature is on."""
    network = {"members": ["evolve"], "bots": {}}
    expected = deploy.expected_plist_labels(network)
    for label in deploy.FEATURE_GATED_PLIST_LABELS:
        assert label in expected, (
            f"{label} is feature-gated but missing from expected_plist_labels; "
            "orphan-sweeper would delete it the next time the feature is on"
        )


def test_unknown_label_returns_false(tmp_path):
    """Non-gated labels should always return False — absence is a real
    failure for them and the health check must FAIL as before."""
    install_json = _write_install_json(tmp_path, {"feature_profile": "standard"})
    assert deploy.is_label_feature_gated_off(
        "ai.evolve.evolve.heal", install_json,
    ) is False


def test_gated_feature_off_returns_true(tmp_path):
    """Standard-profile install (the household default): the gate is off,
    so an absent plist is intentional — health check should skip the FAIL."""
    install_json = _write_install_json(tmp_path, {"feature_profile": "standard"})
    assert deploy.is_label_feature_gated_off(
        "ai.evolve.evolve.upstream-issues-watcher", install_json,
    ) is True
    assert deploy.is_label_feature_gated_off(
        "ai.evolve.evolve.inbound-issues-watcher", install_json,
    ) is True


def test_gated_feature_on_via_profile_returns_false(tmp_path):
    """Developer-profile install opts both watchers on by default — the
    plists SHOULD be present and absence IS a real failure."""
    install_json = _write_install_json(tmp_path, {"feature_profile": "developer"})
    assert deploy.is_label_feature_gated_off(
        "ai.evolve.evolve.upstream-issues-watcher", install_json,
    ) is False
    assert deploy.is_label_feature_gated_off(
        "ai.evolve.evolve.inbound-issues-watcher", install_json,
    ) is False


def test_gated_feature_on_via_explicit_override_returns_false(tmp_path):
    """Explicit override > profile default. Standard-profile install with
    an explicit enable for the watcher means the plist SHOULD be present."""
    install_json = _write_install_json(tmp_path, {
        "feature_profile": "standard",
        "features": {"upstream_issues_watcher": {"enabled": True}},
    })
    assert deploy.is_label_feature_gated_off(
        "ai.evolve.evolve.upstream-issues-watcher", install_json,
    ) is False


def test_gated_feature_off_via_explicit_override_returns_true(tmp_path):
    """Symmetric: developer profile with an explicit off override means
    the plist is intentionally absent — no FAIL."""
    install_json = _write_install_json(tmp_path, {
        "feature_profile": "developer",
        "features": {"upstream_issues_watcher": {"enabled": False}},
    })
    assert deploy.is_label_feature_gated_off(
        "ai.evolve.evolve.upstream-issues-watcher", install_json,
    ) is True


def test_missing_install_json_treated_as_default_profile(tmp_path):
    """No install.json yet (very fresh install): the default profile is
    standard, both watchers are off, so the gate skip applies."""
    install_json = tmp_path / "no-such-install.json"
    assert deploy.is_label_feature_gated_off(
        "ai.evolve.evolve.upstream-issues-watcher", install_json,
    ) is True
