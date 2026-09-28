"""Tests for the install-infra-jobs gate on upstream_issues_watcher.

Contract under test:
  - ``_maybe_install_launchd_upstream_issues_watcher`` calls
    ``_install_launchd`` IFF ``install_profile.is_feature_enabled`` returns
    True for the feature name.
  - When the feature is off, the operator gets a clear log line — not silence
    (so re-running install-infra-jobs after flipping the feature is
    self-explanatory).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import deploy  # noqa: E402


def _make_install_json(tmp_path: Path, *, enabled: bool) -> Path:
    """Write a minimal install.json under a tmp shared_dir; return shared_dir."""
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    install_json = shared_dir / "install.json"
    install_json.write_text(json.dumps({
        "feature_profile": "developer" if enabled else "standard",
        "features": {
            "upstream_issues_watcher": {"enabled": enabled},
        },
    }))
    return shared_dir


def test_feature_off_does_not_call_install_launchd(tmp_path):
    """Standard-profile install never deploys the upstream_issues_watcher plist."""
    shared_dir = _make_install_json(tmp_path, enabled=False)
    result = deploy.DeployResult(bot_id="evolve", success=True)

    with patch.object(deploy, "_install_launchd") as install_mock:
        deploy._maybe_install_launchd_upstream_issues_watcher(
            result, shared_dir=shared_dir,
        )
        assert install_mock.call_count == 0

    # The operator should still get a log line — silent skip is bad UX.
    log_text = "\n".join(result.steps)
    assert "upstream-issues-watcher" in log_text
    assert "feature off" in log_text or "skipped" in log_text


def test_feature_on_calls_install_launchd_with_expected_label(tmp_path):
    """When the feature is enabled, the launchd job lands with the canonical
    label + script path."""
    shared_dir = _make_install_json(tmp_path, enabled=True)
    result = deploy.DeployResult(bot_id="evolve", success=True)

    with patch.object(deploy, "_install_launchd") as install_mock:
        deploy._maybe_install_launchd_upstream_issues_watcher(
            result, shared_dir=shared_dir,
        )
        assert install_mock.call_count == 1
        kwargs = install_mock.call_args.kwargs
        assert kwargs["label"] == "ai.evolve.evolve.upstream-issues-watcher"
        assert kwargs["user"] == "evolve"
        assert kwargs["script_path"].name == "upstream_issues_watcher.py"
        # 15-min interval; deeper cadence-value assertion would couple the
        # test to a number we expect to tune over time — assert presence not
        # exact value.
        assert "interval" in kwargs["schedule"]
        assert kwargs["schedule"]["interval"] >= 60  # sanity floor


def test_feature_on_logs_gh_auth_instructions(tmp_path):
    """Operator-facing UX: when we install the plist, we ALSO print the
    one-time gh-auth-login instruction. Without this, the script will
    silently emit auth Signals for hours before anyone notices."""
    shared_dir = _make_install_json(tmp_path, enabled=True)
    result = deploy.DeployResult(bot_id="evolve", success=True)

    with patch.object(deploy, "_install_launchd"):
        deploy._maybe_install_launchd_upstream_issues_watcher(
            result, shared_dir=shared_dir,
        )

    log_text = "\n".join(result.steps)
    assert "gh auth login" in log_text
    assert "sudo -u evolve" in log_text


def test_import_failure_does_not_crash_install(tmp_path):
    """If install_profile somehow can't be imported (e.g. corrupt analyzer
    tree, shadow issue), the gate must noop with a warning rather than
    propagating the exception and breaking the entire install-infra-jobs
    run."""
    shared_dir = _make_install_json(tmp_path, enabled=True)
    result = deploy.DeployResult(bot_id="evolve", success=True)

    # Sabotage the lazy import by deleting any cached module.
    sys.modules.pop("install_profile", None)
    # Then patch the import machinery so the import fails.
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def block_install_profile(name, *args, **kwargs):
        if name == "install_profile":
            raise ImportError("simulated import failure")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=block_install_profile):
        with patch.object(deploy, "_install_launchd") as install_mock:
            deploy._maybe_install_launchd_upstream_issues_watcher(
                result, shared_dir=shared_dir,
            )
            assert install_mock.call_count == 0

    log_text = "\n".join(result.steps)
    # The operator should see a [warn] line so debugging is possible.
    assert "[warn]" in log_text
    assert "install_profile" in log_text
