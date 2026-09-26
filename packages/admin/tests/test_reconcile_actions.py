"""Tests for ``applications.reconcile_actions``.

Three layers:

- Unit tests on ``_classify`` (the drift-detection state machine) — every
  classification case has a focused test so a future refactor can't
  silently re-tier any of them.
- ``_normalize_action_for_compare`` — stamp stripping is what makes the
  comparison meaningful; if it regresses, every installed action looks
  drifted.
- End-to-end ``reconcile_actions()`` happy paths + ``--apply`` wiring
  with apply_actions mocked, plus the filter / no-pkg / side-loaded /
  bot-not-found edge cases.

Mocks the gallery loader and apply_actions so tests don't touch disk
or shell out.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.reconcile_actions import (  # noqa: E402
    CLASS_OK,
    CLASS_SHAPE_DRIFT,
    CLASS_MISSING_IN_INSTALLED,
    CLASS_MISSING_IN_GALLERY,
    CLASS_SKIPPED_NO_PKG_ID,
    CLASS_SKIPPED_SIDE_LOADED,
    CLASS_SKIPPED_NO_DAEMON,
    CLASS_ERROR,
    ReconcileResult,
    AppDriftReport,
    _classify,
    _normalize_action_for_compare,
    reconcile_actions,
)


# ── _normalize_action_for_compare ────────────────────────────────────────────


def test_normalize_strips_install_stamps() -> None:
    a = {
        "id":                 "x",
        "mechanism":          "launchd",
        "install":            {"plist_label": "ai.evolve.atlas.x"},
        "installed_at":       "2026-06-01T00:00:00Z",
        "installed_by":       "forge:j-abc",
        "installed_artifact": "/Library/LaunchDaemons/x.plist",
    }
    n = _normalize_action_for_compare(a)
    assert n == {
        "id":        "x",
        "mechanism": "launchd",
        "install":   {"plist_label": "ai.evolve.atlas.x"},
    }


def test_normalize_tolerates_non_dict() -> None:
    assert _normalize_action_for_compare(None) == {}
    assert _normalize_action_for_compare("string") == {}
    assert _normalize_action_for_compare(42) == {}


# ── _classify — every classification case ────────────────────────────────────


def test_classify_no_daemon_in_either_side() -> None:
    cls, detail, drifted = _classify([], [])
    assert cls == CLASS_SKIPPED_NO_DAEMON
    assert "neither side" in detail or "scheduled_actions" in detail
    assert drifted == []


def test_classify_ok_when_shape_matches() -> None:
    installed = [{"id": "a", "mechanism": "launchd",
                  "install": {"plist_label": "x"}}]
    gallery = [{"id": "a", "mechanism": "launchd",
                "install": {"plist_label": "x"}}]
    cls, detail, drifted = _classify(installed, gallery)
    assert cls == CLASS_OK
    assert drifted == []


def test_classify_ok_when_only_difference_is_install_stamps() -> None:
    """Stamps (installed_at/by/artifact) must NOT count as drift."""
    installed = [{
        "id": "a", "mechanism": "launchd",
        "install": {"plist_label": "x"},
        "installed_at": "2026-06-01T00:00:00Z",
        "installed_by": "forge:j-old",
        "installed_artifact": "/Library/LaunchDaemons/x.plist",
    }]
    gallery = [{"id": "a", "mechanism": "launchd",
                "install": {"plist_label": "x"}}]
    cls, _, _ = _classify(installed, gallery)
    assert cls == CLASS_OK


def test_classify_shape_drift_when_install_block_differs() -> None:
    """The 2026-06-04 namespace rename lands here: same action id, new
    plist_label in gallery."""
    installed = [{"id": "a", "mechanism": "launchd",
                  "install": {"plist_label": "com.${bot_id}.foo"}}]
    gallery = [{"id": "a", "mechanism": "launchd",
                "install": {"plist_label": "ai.evolve.${bot_id}.foo"}}]
    cls, detail, drifted = _classify(installed, gallery)
    assert cls == CLASS_SHAPE_DRIFT
    assert "a" in drifted
    assert "changed shape" in detail


def test_classify_missing_in_installed_for_new_gallery_action() -> None:
    """The 2026-06-04 first migration: installed manifests had
    scheduled_actions=[], gallery now has structured entries."""
    installed = []
    gallery = [{"id": "morning", "mechanism": "launchd", "install": {}}]
    cls, detail, drifted = _classify(installed, gallery)
    assert cls == CLASS_MISSING_IN_INSTALLED
    assert "morning" in drifted
    assert "1 action" in detail


def test_classify_missing_in_gallery_for_orphan_installed_action() -> None:
    installed = [{"id": "orphan", "mechanism": "launchd", "install": {}}]
    gallery = []
    cls, detail, drifted = _classify(installed, gallery)
    assert cls == CLASS_MISSING_IN_GALLERY
    assert "orphan" in drifted


def test_classify_missing_in_installed_when_both_sides_have_uniques() -> None:
    """If gallery has new + installed has orphans, prefer
    MISSING_IN_INSTALLED so --apply does the right thing (sync from
    gallery picks up the new entries and removes the orphans)."""
    installed = [{"id": "old-orphan", "mechanism": "launchd", "install": {}}]
    gallery = [{"id": "new-action", "mechanism": "launchd", "install": {}}]
    cls, detail, drifted = _classify(installed, gallery)
    assert cls == CLASS_MISSING_IN_INSTALLED
    assert "new-action" in drifted
    assert "old-orphan" in drifted
    assert "new-action" in detail
    assert "old-orphan" in detail


def test_classify_shape_drift_lists_both_changed_and_new() -> None:
    """Drift on one + new in gallery: classification stays SHAPE_DRIFT
    (more specific), but detail mentions both."""
    installed = [{"id": "a", "mechanism": "launchd",
                  "install": {"plist_label": "old"}}]
    gallery = [
        {"id": "a", "mechanism": "launchd", "install": {"plist_label": "new"}},
        {"id": "b", "mechanism": "launchd", "install": {}},
    ]
    cls, detail, drifted = _classify(installed, gallery)
    assert cls == CLASS_SHAPE_DRIFT
    assert "a" in drifted
    assert "b" in drifted
    assert "changed shape" in detail
    assert "new action" in detail


def test_classify_skips_non_dict_actions() -> None:
    """Defensive against on-disk corruption: non-dict entries silently
    dropped from comparison."""
    installed = [{"id": "a", "mechanism": "launchd", "install": {}}, None, 42]
    gallery = [{"id": "a", "mechanism": "launchd", "install": {}}, "bad"]
    cls, _, _ = _classify(installed, gallery)
    assert cls == CLASS_OK


# ── ReconcileResult shape ────────────────────────────────────────────────────


def test_reconcile_result_by_classification_aggregates() -> None:
    r = ReconcileResult(reports=[
        AppDriftReport(bot_id="a", app_id="x", pkg_id="p", classification=CLASS_OK),
        AppDriftReport(bot_id="a", app_id="y", pkg_id="p", classification=CLASS_SHAPE_DRIFT),
        AppDriftReport(bot_id="b", app_id="x", pkg_id="p", classification=CLASS_SHAPE_DRIFT),
        AppDriftReport(bot_id="b", app_id="z", pkg_id="", classification=CLASS_SKIPPED_NO_PKG_ID),
    ])
    assert r.by_classification == {
        CLASS_OK:                1,
        CLASS_SHAPE_DRIFT:       2,
        CLASS_SKIPPED_NO_PKG_ID: 1,
    }
    # drifted_count counts shape_drift but not OK or skipped — the
    # operator-actionable count.
    assert r.drifted_count == 2


def test_reconcile_result_apply_counts() -> None:
    r = ReconcileResult(applied=True, reports=[
        AppDriftReport(bot_id="a", app_id="x", pkg_id="p",
                       classification=CLASS_SHAPE_DRIFT,
                       applied=True,
                       apply_summary={"counts": {"ok": 1, "failed": 0, "skipped": 0}}),
        AppDriftReport(bot_id="b", app_id="y", pkg_id="p",
                       classification=CLASS_MISSING_IN_INSTALLED,
                       applied=True,
                       apply_summary={"counts": {"ok": 0, "failed": 1, "skipped": 0}}),
        AppDriftReport(bot_id="c", app_id="z", pkg_id="p",
                       classification=CLASS_SHAPE_DRIFT,
                       applied=True,
                       apply_error="bot not registered"),
    ])
    assert r.apply_succeeded_count == 1
    assert r.apply_failed_count == 2  # one with failed:1, one with error


def test_reconcile_result_to_dict_pins_shape() -> None:
    """--json output relies on to_dict's shape; pin it so renames in
    AppDriftReport / ReconcileResult break loudly."""
    r = ReconcileResult(reports=[
        AppDriftReport(
            bot_id="atlas", app_id="atlas-daily-digest", pkg_id="p-7b26ba5e",
            classification=CLASS_SHAPE_DRIFT,
            detail="action a changed",
            drifted_action_ids=["a"],
        ),
    ])
    d = r.to_dict()
    assert d["applied"] is False
    assert d["summary"]["total"] == 1
    assert d["summary"]["drifted"] == 1
    assert d["summary"]["by_classification"] == {CLASS_SHAPE_DRIFT: 1}
    report = d["reports"][0]
    assert report["classification"] == CLASS_SHAPE_DRIFT
    assert report["drifted_action_ids"] == ["a"]


# ── End-to-end reconcile_actions ────────────────────────────────────────────


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    return tmp_path / "evolve"


def _patch_workspace_resolution(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Redirect get_bot_workspace to a tmp_path."""
    from evolve_admin import config as config_mod
    monkeypatch.setattr(
        config_mod, "get_bot_workspace",
        lambda bot_id, user=None: root / "Users" / (user or bot_id) / ".openclaw" / "workspace",
    )


def _write_manifest(root: Path, bot: str, app_id: str, **fields) -> None:
    """Drop an installed manifest at the bot's workspace path."""
    mdir = root / "Users" / bot / ".openclaw" / "workspace" / "manifests"
    mdir.mkdir(parents=True, exist_ok=True)
    m = {
        "id": app_id, "name": app_id, "bot_id": bot,
        "status": "approved", "pkg_id": "p-fake",
        "scheduled_actions": [], "files": [],
        **fields,
    }
    (mdir / f"{app_id}.json").write_text(json.dumps(m, indent=2))


def test_reconcile_reports_no_drift_when_gallery_matches(
    tmp_path: Path, shared_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_manifest(tmp_path, "atlas", "atlas-daily-digest",
                    pkg_id="p-7b26ba5e",
                    scheduled_actions=[
                        {"id": "x", "mechanism": "launchd", "install": {"plist_label": "ai.evolve.atlas.x"}},
                    ])
    _patch_workspace_resolution(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "evolve_admin.applications.reconcile_actions.load_gallery_package"
        if False else "evolve_admin.applications.gallery.load_gallery_package",
        lambda pid, sd: {"pkg_id": pid, "scheduled_actions": [
            {"id": "x", "mechanism": "launchd", "install": {"plist_label": "ai.evolve.atlas.x"}},
        ]},
    )

    result = reconcile_actions(
        shared_dir,
        network={"bots": {"atlas": {"user": "atlas"}}},
    )
    assert len(result.reports) == 1
    assert result.reports[0].classification == CLASS_OK
    assert result.drifted_count == 0


def test_reconcile_detects_shape_drift(
    tmp_path: Path, shared_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The post-#2167 case: installed has com.${bot}.* labels, gallery
    has ai.evolve.${bot}.*."""
    _write_manifest(tmp_path, "atlas", "atlas-daily-digest",
                    pkg_id="p-7b26ba5e",
                    scheduled_actions=[
                        {"id": "x", "mechanism": "launchd",
                         "install": {"plist_label": "com.${bot_id}.atlas-daily-digest"}},
                    ])
    _patch_workspace_resolution(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "evolve_admin.applications.gallery.load_gallery_package",
        lambda pid, sd: {"pkg_id": pid, "scheduled_actions": [
            {"id": "x", "mechanism": "launchd",
             "install": {"plist_label": "ai.evolve.${bot_id}.atlas-daily-digest"}},
        ]},
    )

    result = reconcile_actions(
        shared_dir,
        network={"bots": {"atlas": {"user": "atlas"}}},
    )
    assert result.reports[0].classification == CLASS_SHAPE_DRIFT
    assert "x" in result.reports[0].drifted_action_ids
    assert result.drifted_count == 1


def test_reconcile_skips_side_loaded_packages(
    tmp_path: Path, shared_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pkg_id present but not in the gallery (Atlas-pre-move case) →
    skipped_side_loaded, not error."""
    _write_manifest(tmp_path, "atlas", "side-loaded-app",
                    pkg_id="p-not-in-gallery",
                    scheduled_actions=[
                        {"id": "x", "mechanism": "launchd", "install": {}}
                    ])
    _patch_workspace_resolution(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "evolve_admin.applications.gallery.load_gallery_package",
        lambda pid, sd: None,
    )

    result = reconcile_actions(
        shared_dir,
        network={"bots": {"atlas": {"user": "atlas"}}},
    )
    assert result.reports[0].classification == CLASS_SKIPPED_SIDE_LOADED


def test_reconcile_skips_manifests_with_no_pkg_id(
    tmp_path: Path, shared_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_manifest(tmp_path, "atlas", "custom-app",
                    pkg_id="", scheduled_actions=[])
    _patch_workspace_resolution(monkeypatch, tmp_path)
    # gallery loader shouldn't even be called.
    loader = mock.MagicMock(return_value=None)
    monkeypatch.setattr(
        "evolve_admin.applications.gallery.load_gallery_package",
        loader,
    )
    result = reconcile_actions(
        shared_dir,
        network={"bots": {"atlas": {"user": "atlas"}}},
    )
    assert result.reports[0].classification == CLASS_SKIPPED_NO_PKG_ID
    loader.assert_not_called()


def test_reconcile_bot_filter_unknown_bot_surfaces_error(
    shared_dir: Path,
) -> None:
    result = reconcile_actions(
        shared_dir,
        bot_filter="ghost",
        network={"bots": {"atlas": {"user": "atlas"}}},
    )
    assert len(result.reports) == 1
    assert result.reports[0].classification == CLASS_ERROR
    assert "ghost" in result.reports[0].detail


def test_reconcile_apply_calls_apply_actions_for_drifted_only(
    tmp_path: Path, shared_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--apply must invoke apply_actions on drifted entries (shape_drift,
    missing_in_installed) but NOT on OK / skipped / missing_in_gallery."""
    # Write three manifests, one of each shape.
    _write_manifest(tmp_path, "atlas", "ok-app",
                    pkg_id="p-1",
                    scheduled_actions=[
                        {"id": "a", "mechanism": "launchd", "install": {"plist_label": "x"}},
                    ])
    _write_manifest(tmp_path, "atlas", "drifted-app",
                    pkg_id="p-2",
                    scheduled_actions=[
                        {"id": "a", "mechanism": "launchd", "install": {"plist_label": "old"}},
                    ])
    _write_manifest(tmp_path, "atlas", "orphan-app",
                    pkg_id="p-3",
                    scheduled_actions=[
                        {"id": "z", "mechanism": "launchd", "install": {}},
                    ])
    _patch_workspace_resolution(monkeypatch, tmp_path)

    gallery_lookup = {
        "p-1": {"scheduled_actions": [
            {"id": "a", "mechanism": "launchd", "install": {"plist_label": "x"}},
        ]},  # OK
        "p-2": {"scheduled_actions": [
            {"id": "a", "mechanism": "launchd", "install": {"plist_label": "new"}},
        ]},  # SHAPE_DRIFT
        "p-3": {"scheduled_actions": []},  # MISSING_IN_GALLERY
    }
    monkeypatch.setattr(
        "evolve_admin.applications.gallery.load_gallery_package",
        lambda pid, sd: gallery_lookup.get(pid),
    )

    # Stub apply_actions so we capture which apps it ran against.
    calls: list[str] = []
    from evolve_admin.applications.apply_actions import ApplyActionsResult
    def _stub_apply(bot_id, app_id, sd, *, from_gallery, network=None):
        calls.append(app_id)
        return ApplyActionsResult(
            bot_id=bot_id, app_id=app_id, pkg_id="p",
            summary=[{"action_id": "a", "status": "ok"}],
            synced_from_gallery=True,
        )
    monkeypatch.setattr(
        "evolve_admin.applications.apply_actions.apply_actions",
        _stub_apply,
    )

    result = reconcile_actions(
        shared_dir,
        apply=True,
        network={"bots": {"atlas": {"user": "atlas"}}},
    )

    # apply_actions called only against the drifted entry — not OK, not
    # MISSING_IN_GALLERY (ambiguous, operator must decide).
    assert calls == ["drifted-app"]

    by_app = {r.app_id: r for r in result.reports}
    assert by_app["ok-app"].applied is False
    assert by_app["drifted-app"].applied is True
    assert by_app["drifted-app"].apply_summary is not None
    assert by_app["orphan-app"].applied is False
