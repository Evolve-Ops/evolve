"""Tests for the ``evolve-admin features`` command group.

Covers:
  - ``features list`` — reports profile + per-feature state, including
    distinguishing "explicit override" from "profile default"
  - ``features set <name> on|off`` — writes install.json::features.<name>.enabled
  - ``features set-profile <profile>`` — writes install.json::feature_profile
  - All three commands work on a pod with no pre-existing install.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
_ANALYZER_PKG = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_PKG) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_PKG))

from evolve_admin import cli as cli_mod  # noqa: E402
from evolve_admin.cli import main  # noqa: E402


@pytest.fixture()
def tmp_install_json(tmp_path: Path, monkeypatch):
    """Redirect the CLI's install.json path at a tmp file for the duration
    of the test, isolating it from any real /Users/Shared/evolve state."""
    p = tmp_path / "install.json"
    monkeypatch.setattr(cli_mod, "_INSTALL_JSON_PATH", p)
    return p


def test_features_list_on_clean_pod(tmp_install_json):
    """No install.json yet → reports default profile + every catalogued
    feature as off."""
    runner = CliRunner()
    result = runner.invoke(main, ["features", "list"])
    assert result.exit_code == 0, result.output
    assert "feature_profile: standard" in result.output
    # The motivating power feature should be listed and off-by-default.
    assert "upstream_issues_watcher" in result.output
    assert "off" in result.output


def test_features_list_reports_developer_profile_default(tmp_install_json):
    """Developer profile flips the upstream_issues_watcher default to on."""
    tmp_install_json.write_text(json.dumps({"feature_profile": "developer"}))
    runner = CliRunner()
    result = runner.invoke(main, ["features", "list"])
    assert result.exit_code == 0, result.output
    assert "feature_profile: developer" in result.output
    # Profile default should render as such.
    assert "profile default" in result.output


def test_features_list_distinguishes_explicit_override(tmp_install_json):
    """An explicit features.<name>.enabled value should be labeled as
    'explicit' in the output, not 'profile default'."""
    tmp_install_json.write_text(json.dumps({
        "feature_profile": "standard",
        "features": {"upstream_issues_watcher": {"enabled": True}},
    }))
    runner = CliRunner()
    result = runner.invoke(main, ["features", "list"])
    assert result.exit_code == 0, result.output
    # Resolved to on, despite standard profile.
    line = [ln for ln in result.output.splitlines() if "upstream_issues_watcher" in ln]
    assert line, "feature missing from output"
    assert "on" in line[0]
    assert "explicit" in line[0].lower()


def test_features_set_writes_explicit_flag(tmp_install_json):
    """`features set <name> on` should land an explicit override in
    install.json without clobbering other fields."""
    tmp_install_json.write_text(json.dumps({
        "version": "0.3.0",
        "network_id": "my-pod",
        "feature_profile": "standard",
    }))
    runner = CliRunner()
    result = runner.invoke(main, ["features", "set", "upstream_issues_watcher", "on"])
    assert result.exit_code == 0, result.output
    data = json.loads(tmp_install_json.read_text())
    assert data["features"]["upstream_issues_watcher"]["enabled"] is True
    # Existing fields preserved.
    assert data["version"] == "0.3.0"
    assert data["network_id"] == "my-pod"
    assert data["feature_profile"] == "standard"


def test_features_set_off_overrides_developer_default(tmp_install_json):
    tmp_install_json.write_text(json.dumps({"feature_profile": "developer"}))
    runner = CliRunner()
    result = runner.invoke(main, ["features", "set", "upstream_issues_watcher", "off"])
    assert result.exit_code == 0, result.output
    data = json.loads(tmp_install_json.read_text())
    assert data["features"]["upstream_issues_watcher"]["enabled"] is False


def test_features_set_creates_install_json_on_clean_pod(tmp_install_json):
    """If install.json doesn't exist yet, `features set` should create it
    rather than crash."""
    assert not tmp_install_json.exists()
    runner = CliRunner()
    result = runner.invoke(main, ["features", "set", "upstream_issues_watcher", "on"])
    assert result.exit_code == 0, result.output
    assert tmp_install_json.exists()
    data = json.loads(tmp_install_json.read_text())
    assert data["features"]["upstream_issues_watcher"]["enabled"] is True


def test_features_set_profile_writes_profile_field(tmp_install_json):
    tmp_install_json.write_text(json.dumps({"version": "0.3.0"}))
    runner = CliRunner()
    result = runner.invoke(main, ["features", "set-profile", "developer"])
    assert result.exit_code == 0, result.output
    data = json.loads(tmp_install_json.read_text())
    assert data["feature_profile"] == "developer"
    assert data["version"] == "0.3.0"


def test_features_set_profile_rejects_invalid_profile(tmp_install_json):
    runner = CliRunner()
    result = runner.invoke(main, ["features", "set-profile", "ultra-mode"])
    # click's Choice validator should reject this with a non-zero exit code.
    assert result.exit_code != 0
    # And install.json should not have been written.
    assert not tmp_install_json.exists()


def test_features_set_rejects_invalid_state(tmp_install_json):
    runner = CliRunner()
    result = runner.invoke(main, ["features", "set", "upstream_issues_watcher", "maybe"])
    assert result.exit_code != 0


def test_features_set_then_list_round_trip(tmp_install_json):
    """The end-to-end UX: set a feature, then list — the list should
    reflect what set wrote."""
    runner = CliRunner()
    r1 = runner.invoke(main, ["features", "set", "upstream_issues_watcher", "on"])
    assert r1.exit_code == 0, r1.output
    r2 = runner.invoke(main, ["features", "list"])
    assert r2.exit_code == 0, r2.output
    line = [ln for ln in r2.output.splitlines() if "upstream_issues_watcher" in ln]
    assert line and "on" in line[0]
