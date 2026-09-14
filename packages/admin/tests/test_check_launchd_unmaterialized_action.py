"""End-to-end tests for the realized-only launchd view in ``_check_launchd``.

The 2026-06-17 false alert: atlas's ``task-manager.json`` is a thin v7-arc
instance whose gallery Spec (``p-9bfa1c84``) declares two ``launchd`` scheduled
actions. ``hydrate_v7_arc_instance`` overlays ``scheduled_actions`` from the
Spec, but the app was adopted by the SCANNER (``installed_by: scanner_promote``),
which does NOT run forge's ``_materialize_scheduled_actions`` — so neither
action has an ``installed_artifact`` stamp and neither plist is on
``/Library/LaunchDaemons/``. ``expected_plist_labels``' bare ``plist_label``
fallback still manufactured ``ai.evolve.atlas.task-manager-{cache-urgent,
daily-prune}``, and ``_check_launchd`` fired two perpetual false
``pod_health_launchd`` "Plist not found" Signals.

Unlike ``test_check_launchd_feature_gated.py`` (which stubs
``expected_plist_labels``), these tests drive the REAL function with
``realized_only=True`` so the wiring between ``_check_launchd`` and the
realized-only view is exercised end to end.

Companion unit tests for the param itself live in
``test_expected_plist_labels_scheduled_actions.py``. The orphan-sweeper side
(broad default set unchanged) is pinned there too.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import deploy as _deploy  # noqa: E402
from evolve_admin import health  # noqa: E402
from evolve_admin.applications.manifest import ApplicationManifest  # noqa: E402


def _manifest(bot_id: str, app_id: str, scheduled_actions: list) -> ApplicationManifest:
    m = ApplicationManifest(id=app_id, name=app_id, bot_id=bot_id)
    m.scheduled_actions = scheduled_actions
    return m


def _patch_list_manifests(monkeypatch: pytest.MonkeyPatch, per_bot: dict) -> None:
    from evolve_admin.applications import manifest as manifest_mod

    def fake(shared_dir, bot_id):
        return per_bot.get(bot_id, [])

    monkeypatch.setattr(manifest_mod, "list_manifests", fake)


def _seed(tmp_path: Path) -> tuple[Path, dict]:
    """tmp shared dir + a tmp /Library/LaunchDaemons surrogate. The launchd
    dir is named ``LaunchDaemons`` so a stamped ``/Library/LaunchDaemons/...``
    artifact still classifies as a daemon (parent-name comparison)."""
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "install.json").write_text(json.dumps({"feature_profile": "standard"}))
    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    network = {"sharedDir": str(shared), "members": ["atlas"], "bots": {}}
    return launchd, network


def _fails_mentioning(report: health.HealthReport, needle: str) -> list:
    return [
        c for c in report.checks
        if c.status == health.FAIL and needle in c.name
    ]


def test_pod_health_does_not_fire_for_unmaterialized_hydrated_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact 2026-06-17 false alert. Two hydrated-spec launchd actions with
    plist_label but no installed_artifact (scanner-adopted, never materialized)
    must produce NO "Plist not found" FAIL."""
    launchd, network = _seed(tmp_path)
    monkeypatch.setattr(_deploy, "LAUNCHD_DIR", launchd)
    monkeypatch.setattr(health, "LAUNCHD_DIR", launchd)
    _patch_list_manifests(monkeypatch, {
        "atlas": [_manifest("atlas", "task-manager", [
            {
                "id": "task-manager-cache-urgent",
                "mechanism": "launchd",
                "install": {"plist_label": "ai.evolve.${bot_id}.task-manager-cache-urgent"},
            },
            {
                "id": "task-manager-daily-prune",
                "mechanism": "launchd",
                "install": {"plist_label": "ai.evolve.${bot_id}.task-manager-daily-prune"},
            },
        ])],
    })

    report = health.HealthReport()
    with patch.object(health, "_launchd_loaded", return_value=True), \
         patch.object(health, "_puller_log_is_stale", return_value=False):
        health._check_launchd(report, network)

    assert _fails_mentioning(report, "task-manager-cache-urgent") == [], (
        "pod_health fired a false 'Plist not found' for a never-materialized "
        "hydrated-spec launchd action"
    )
    assert _fails_mentioning(report, "task-manager-daily-prune") == []


def test_pod_health_still_fires_for_stamped_missing_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """True-positive preserved: a stamped LaunchDaemon artifact whose plist is
    NOT on disk is a real broken install — pod_health must still FAIL."""
    launchd, network = _seed(tmp_path)
    monkeypatch.setattr(_deploy, "LAUNCHD_DIR", launchd)
    monkeypatch.setattr(health, "LAUNCHD_DIR", launchd)
    # Stamped, but we deliberately do NOT create the plist on disk.
    _patch_list_manifests(monkeypatch, {
        "atlas": [_manifest("atlas", "atlas-daily-digest", [{
            "id": "atlas-daily-digest",
            "mechanism": "launchd",
            "installed_artifact":
                "/Library/LaunchDaemons/ai.evolve.atlas.atlas-daily-digest.plist",
        }])],
    })

    report = health.HealthReport()
    with patch.object(health, "_launchd_loaded", return_value=True), \
         patch.object(health, "_puller_log_is_stale", return_value=False):
        health._check_launchd(report, network)

    fails = _fails_mentioning(report, "ai.evolve.atlas.atlas-daily-digest")
    assert len(fails) == 1, (
        "a stamped-but-missing plist must still FAIL (true positive); checks="
        f"{[(c.status, c.name) for c in report.checks]}"
    )
    assert "Plist not found" in fails[0].detail


def test_pod_health_fires_for_missing_realized_when_sibling_unstamped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed app, end to end: the stamped action's missing plist FAILs while the
    sibling unstamped action contributes no false FAIL."""
    launchd, network = _seed(tmp_path)
    monkeypatch.setattr(_deploy, "LAUNCHD_DIR", launchd)
    monkeypatch.setattr(health, "LAUNCHD_DIR", launchd)
    _patch_list_manifests(monkeypatch, {
        "atlas": [_manifest("atlas", "task-manager", [
            {
                "id": "task-manager-cache-urgent",
                "mechanism": "launchd",
                "install": {"plist_label": "ai.evolve.${bot_id}.task-manager-cache-urgent"},
            },
            {
                "id": "task-manager-daily-prune",
                "mechanism": "launchd",
                "installed_artifact":
                    "/Library/LaunchDaemons/ai.evolve.atlas.task-manager-daily-prune.plist",
            },
        ])],
    })

    report = health.HealthReport()
    with patch.object(health, "_launchd_loaded", return_value=True), \
         patch.object(health, "_puller_log_is_stale", return_value=False):
        health._check_launchd(report, network)

    assert _fails_mentioning(report, "task-manager-cache-urgent") == []
    stamped_fails = _fails_mentioning(report, "task-manager-daily-prune")
    assert len(stamped_fails) == 1
    assert "Plist not found" in stamped_fails[0].detail
