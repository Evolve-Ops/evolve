"""Tests for the install.json feature toggle helper.

Covers:
  - list_features / get_feature_status read-side behavior
  - set_feature_enabled writes the override + invokes the launchd
    install/uninstall handler
  - the inbound_issues_watcher catalog entry is wired correctly so a
    fresh-install UI toggle ends up installing the plist
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ── PROFILE_DEFAULTS catalog ────────────────────────────────────────────────


def test_inbound_watcher_is_dev_profile_default():
    """The inbound watcher must default-on for developer-tier installs
    so a fresh `feature_profile=developer` install gets it without an
    extra toggle step. Standard installs still stay off."""
    from install_profile import PROFILE_DEFAULTS
    assert "inbound_issues_watcher" in PROFILE_DEFAULTS["developer"]
    assert "inbound_issues_watcher" not in PROFILE_DEFAULTS["standard"]
    assert "inbound_issues_watcher" not in PROFILE_DEFAULTS["minimal"]


# ── Read side ────────────────────────────────────────────────────────────


def test_list_features_includes_inbound_watcher(tmp_path):
    from evolve_admin.feature_toggle import list_features
    out = list_features(shared_dir=tmp_path)
    names = [f["name"] for f in out["features"]]
    assert "inbound_issues_watcher" in names


def test_get_feature_status_off_by_default_on_standard(tmp_path):
    """Fresh install, no install.json → defaults to standard profile,
    inbound watcher off."""
    from evolve_admin.feature_toggle import get_feature_status
    s = get_feature_status("inbound_issues_watcher", shared_dir=tmp_path)
    assert s["enabled"] is False
    assert s["source"] == "off by default"
    assert s["profile"] == "standard"
    assert s["has_launchd_job"] is True
    assert s["on_dev_profile"] is True


def test_get_feature_status_reports_profile_default_when_developer(tmp_path):
    (tmp_path / "install.json").write_text(json.dumps({
        "feature_profile": "developer",
    }))
    from evolve_admin.feature_toggle import get_feature_status
    s = get_feature_status("inbound_issues_watcher", shared_dir=tmp_path)
    assert s["enabled"] is True
    assert s["source"] == "profile default"


def test_get_feature_status_reports_explicit_override(tmp_path):
    """Explicit override beats the profile default — bidirectional."""
    (tmp_path / "install.json").write_text(json.dumps({
        "feature_profile": "developer",  # would default on…
        "features": {
            "inbound_issues_watcher": {"enabled": False},  # …but explicitly off
        },
    }))
    from evolve_admin.feature_toggle import get_feature_status
    s = get_feature_status("inbound_issues_watcher", shared_dir=tmp_path)
    assert s["enabled"] is False
    assert s["source"] == "explicit"


def test_get_feature_status_plist_installed_detected(tmp_path, monkeypatch):
    """The plist-installed flag drives the UI's "running / not running"
    chip. Verify it actually checks the filesystem."""
    from evolve_admin import feature_toggle
    fake_launchd = tmp_path / "LaunchDaemons"
    fake_launchd.mkdir()
    plist = fake_launchd / "ai.evolve.evolve.inbound-issues-watcher.plist"
    plist.write_text("<plist/>")
    monkeypatch.setattr(feature_toggle, "_LAUNCHD_DIR", fake_launchd)
    s = feature_toggle.get_feature_status(
        "inbound_issues_watcher", shared_dir=tmp_path,
    )
    assert s["plist_installed"] is True


def test_get_feature_status_last_activity_from_state_mtime(tmp_path):
    """The state.json mtime is the cheapest "last ran" signal we can
    surface without parsing launchd telemetry."""
    state_dir = tmp_path / "inbound_issues_watcher"
    state_dir.mkdir()
    (state_dir / "state.json").write_text("{}")
    from evolve_admin.feature_toggle import get_feature_status
    s = get_feature_status("inbound_issues_watcher", shared_dir=tmp_path)
    assert s["last_activity_at"] is not None


# ── Write side ───────────────────────────────────────────────────────────


def test_set_feature_enabled_writes_install_json(tmp_path, monkeypatch):
    """Flipping the toggle must durably persist to install.json."""
    from evolve_admin import deploy, feature_toggle
    # Stub the launchd handlers so the test doesn't shell out to launchctl.
    monkeypatch.setattr(deploy, "install_inbound_issues_watcher_now",
                        lambda result, shared: result.log("install stubbed"))
    monkeypatch.setattr(deploy, "uninstall_inbound_issues_watcher_now",
                        lambda result: result.log("uninstall stubbed"))
    feature_toggle.set_feature_enabled(
        "inbound_issues_watcher", True, shared_dir=tmp_path,
    )
    data = json.loads((tmp_path / "install.json").read_text())
    assert data["features"]["inbound_issues_watcher"]["enabled"] is True


def test_set_feature_enabled_invokes_install_handler_on_enable(tmp_path, monkeypatch):
    from evolve_admin import deploy, feature_toggle
    called = []
    monkeypatch.setattr(deploy, "install_inbound_issues_watcher_now",
                        lambda result, shared: called.append(("install", shared)))
    monkeypatch.setattr(deploy, "uninstall_inbound_issues_watcher_now",
                        lambda result: called.append(("uninstall",)))
    feature_toggle.set_feature_enabled(
        "inbound_issues_watcher", True, shared_dir=tmp_path,
    )
    assert called == [("install", tmp_path)]


def test_set_feature_enabled_invokes_uninstall_handler_on_disable(tmp_path, monkeypatch):
    from evolve_admin import deploy, feature_toggle
    called = []
    monkeypatch.setattr(deploy, "install_inbound_issues_watcher_now",
                        lambda result, shared: called.append(("install",)))
    monkeypatch.setattr(deploy, "uninstall_inbound_issues_watcher_now",
                        lambda result: called.append(("uninstall",)))
    feature_toggle.set_feature_enabled(
        "inbound_issues_watcher", False, shared_dir=tmp_path,
    )
    assert called == [("uninstall",)]


def test_set_feature_enabled_unknown_feature_rejected(tmp_path):
    from evolve_admin.feature_toggle import (
        set_feature_enabled, FeatureToggleError,
    )
    with pytest.raises(FeatureToggleError):
        set_feature_enabled("bogus_feature_name", True, shared_dir=tmp_path)


def test_set_feature_enabled_preserves_other_install_json_fields(tmp_path, monkeypatch):
    """The write path must merge with existing data — don't clobber
    feature_profile or other operator-owned fields."""
    (tmp_path / "install.json").write_text(json.dumps({
        "version": "1.2.3",
        "feature_profile": "developer",
        "features": {
            "upstream_issues_watcher": {"enabled": True},
        },
        "network_id": "abc123",
    }))
    from evolve_admin import deploy, feature_toggle
    monkeypatch.setattr(deploy, "install_inbound_issues_watcher_now",
                        lambda result, shared: None)
    feature_toggle.set_feature_enabled(
        "inbound_issues_watcher", True, shared_dir=tmp_path,
    )
    data = json.loads((tmp_path / "install.json").read_text())
    assert data["version"] == "1.2.3"
    assert data["feature_profile"] == "developer"
    assert data["network_id"] == "abc123"
    assert data["features"]["upstream_issues_watcher"]["enabled"] is True
    assert data["features"]["inbound_issues_watcher"]["enabled"] is True


def test_set_feature_enabled_atomic_no_temp_leftovers(tmp_path, monkeypatch):
    from evolve_admin import deploy, feature_toggle
    monkeypatch.setattr(deploy, "install_inbound_issues_watcher_now",
                        lambda result, shared: None)
    feature_toggle.set_feature_enabled(
        "inbound_issues_watcher", True, shared_dir=tmp_path,
    )
    leftovers = list(tmp_path.glob(".install-*"))
    assert leftovers == [], f"temp files leaked: {leftovers}"


def test_set_feature_enabled_returns_refreshed_status(tmp_path, monkeypatch):
    from evolve_admin import deploy, feature_toggle
    monkeypatch.setattr(deploy, "install_inbound_issues_watcher_now",
                        lambda result, shared: None)
    out = feature_toggle.set_feature_enabled(
        "inbound_issues_watcher", True, shared_dir=tmp_path,
    )
    assert out["ok"] is True
    assert out["enabled"] is True
    assert out["status"]["enabled"] is True
    assert out["status"]["source"] == "explicit"


def test_set_feature_enabled_launchd_failure_does_not_corrupt_state(tmp_path, monkeypatch):
    """If the launchd install raises, the install.json override must
    still be durable — operator can re-run install-infra-jobs to retry."""
    from evolve_admin import deploy, feature_toggle

    def boom(*args, **kwargs):
        raise RuntimeError("launchd is sulking")
    monkeypatch.setattr(deploy, "install_inbound_issues_watcher_now", boom)

    out = feature_toggle.set_feature_enabled(
        "inbound_issues_watcher", True, shared_dir=tmp_path,
    )
    # The set call still returns ok=True (install.json saved); the
    # launchd failure is surfaced in the log for the operator.
    assert out["ok"] is True
    assert any("launchd op raised" in line for line in out["log"])
    data = json.loads((tmp_path / "install.json").read_text())
    assert data["features"]["inbound_issues_watcher"]["enabled"] is True
