"""Tests for ``applications.apply_actions``.

Two layers of coverage:

- Unit tests on the precondition checks (bot not registered, manifest
  missing, gallery package missing when --from-gallery is set), the
  gallery-sync stamp-preservation logic, and the synthetic ForgeJob
  shape.

- One end-to-end shape test that exercises the full happy path
  through to ``_materialize_scheduled_actions`` with the install
  helper mocked. Confirms the manifest gets saved with the
  installed_* stamps that Phase 4.5 stamps in-place.

Mocks the install helpers because the real ones shell out (cp, chown,
launchctl); none of that is appropriate to exercise from unit tests.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.apply_actions import (  # noqa: E402
    ApplyActionsError,
    ApplyActionsResult,
    BotNotRegisteredError,
    GalleryPackageNotFoundError,
    ManifestNotFoundError,
    _build_synthetic_job,
    _sync_scheduled_actions_from_gallery,
    apply_actions,
)
from evolve_admin.applications.manifest import (  # noqa: E402
    ApplicationManifest,
    save_manifest,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    """Provide a fake shared_dir that ``save_manifest`` can write under."""
    return tmp_path / "evolve"


@pytest.fixture
def bot_workspace(tmp_path: Path) -> Path:
    """Provide a fake bot workspace so ``load_manifest`` finds a manifest dir."""
    ws = tmp_path / "Users" / "atlas" / ".openclaw" / "workspace" / "manifests"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _write_manifest(workspace_manifests: Path, app_id: str, **fields) -> dict:
    """Write a minimal manifest JSON to the bot's workspace manifests dir.

    Returns the raw dict so tests can assert on shape after apply_actions.
    """
    m = {
        "id":               app_id,
        "name":             app_id.replace("-", " ").title(),
        "bot_id":           "atlas",
        "status":           "approved",
        "pkg_id":           "p-fake0001",
        "scheduled_actions": [],
        "files":            [],
        **fields,
    }
    (workspace_manifests / f"{app_id}.json").write_text(json.dumps(m, indent=2))
    return m


def _patch_workspace_resolution(monkeypatch: pytest.MonkeyPatch, bot_workspace: Path) -> None:
    """Redirect ``load_manifest`` / ``save_manifest`` to find manifests under
    ``bot_workspace`` (the tmp_path equivalent of /Users/<bot>/.openclaw/workspace/manifests/).

    ``manifest._manifests_dir`` imports ``get_bot_workspace`` lazily from
    ``..config``, so we patch the source module — patching the manifest
    module would be a no-op because the function isn't bound there yet.
    """
    workspace_root = bot_workspace.parent  # /Users/atlas/.openclaw/workspace
    from evolve_admin import config as config_mod

    def _fake_workspace(bot_id: str, user: str | None = None):
        return workspace_root

    monkeypatch.setattr(config_mod, "get_bot_workspace", _fake_workspace)


# ── Precondition errors ─────────────────────────────────────────────────────


def test_apply_actions_raises_when_bot_not_registered(
    shared_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "evolve_admin.applications.apply_actions._load_network",
        lambda: {"bots": {}},
        raising=False,
    )
    with pytest.raises(BotNotRegisteredError, match="not registered"):
        apply_actions("nope", "any-app", shared_dir, network={"bots": {}})


def test_apply_actions_raises_when_manifest_missing(
    shared_dir: Path, bot_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_workspace_resolution(monkeypatch, bot_workspace)
    with pytest.raises(ManifestNotFoundError, match="no installed manifest"):
        apply_actions(
            "atlas", "nonexistent-app", shared_dir,
            network={"bots": {"atlas": {"user": "atlas"}}},
        )


def test_apply_actions_raises_when_gallery_missing_with_from_gallery(
    shared_dir: Path, bot_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--from-gallery`` + non-gallery pkg_id (Atlas case) → clean error,
    not a silent fallback."""
    _write_manifest(bot_workspace, "side-loaded-app", pkg_id="p-not-in-gallery")
    _patch_workspace_resolution(monkeypatch, bot_workspace)
    monkeypatch.setattr(
        "evolve_admin.applications.gallery.load_gallery_package",
        lambda pid, sd: None,
    )

    with pytest.raises(GalleryPackageNotFoundError, match="patch the on-disk manifest directly"):
        apply_actions(
            "atlas", "side-loaded-app", shared_dir,
            from_gallery=True,
            network={"bots": {"atlas": {"user": "atlas"}}},
        )


# ── _sync_scheduled_actions_from_gallery ────────────────────────────────────


def test_sync_replaces_actions_and_preserves_stamps() -> None:
    """An action whose id matches keeps installed_at/by/artifact from the
    prior install. A new action (id introduced by migration) gets no
    stamps (Phase 4.5 will stamp it fresh)."""
    manifest = ApplicationManifest(id="x", name="X", bot_id="atlas")
    manifest.scheduled_actions = [
        {
            "id":                  "morning",
            "mechanism":           "launchd",
            "install":             {"plist_label": "old.label"},  # outdated
            "installed_at":        "2026-05-01T00:00:00Z",
            "installed_by":        "forge:j-original-install",
            "installed_artifact":  "/Library/LaunchDaemons/old.label.plist",
        },
    ]
    gallery_pkg = {
        "scheduled_actions": [
            {
                "id":        "morning",
                "mechanism": "launchd",
                "install":   {"plist_label": "new.label"},  # post-migration
            },
            {
                "id":        "premeet",   # new in this version
                "mechanism": "launchd",
                "install":   {"plist_label": "premeet.label"},
            },
        ],
    }

    changed = _sync_scheduled_actions_from_gallery(manifest, gallery_pkg)
    # Both entries count as "changed" relative to the installed manifest:
    #   - "morning" had a different install block.
    #   - "premeet" was new (no prior entry by that id).
    # The counter is a coarse "did anything move" signal for log output,
    # not a fine-grained add/edit/preserve breakdown.
    assert changed == 2

    by_id = {a["id"]: a for a in manifest.scheduled_actions}
    # Stamps preserved on morning.
    assert by_id["morning"]["installed_at"] == "2026-05-01T00:00:00Z"
    assert by_id["morning"]["installed_by"] == "forge:j-original-install"
    # But the install block is the new shape.
    assert by_id["morning"]["install"]["plist_label"] == "new.label"
    # The new premeet entry has no stamps yet.
    assert "installed_at" not in by_id["premeet"]


def test_sync_handles_empty_gallery_actions() -> None:
    """A gallery package with no scheduled_actions[] clears the installed
    list — operator did this deliberately (e.g. an upstream removed the
    daemon). The materializer then does nothing."""
    manifest = ApplicationManifest(id="x", name="X", bot_id="atlas")
    manifest.scheduled_actions = [{"id": "old", "mechanism": "launchd"}]
    _sync_scheduled_actions_from_gallery(manifest, {"scheduled_actions": []})
    assert manifest.scheduled_actions == []


def test_sync_tolerates_malformed_entries() -> None:
    """Non-dict entries in either list are silently skipped — defensive
    against on-disk corruption."""
    manifest = ApplicationManifest(id="x", name="X", bot_id="atlas")
    manifest.scheduled_actions = [{"id": "ok"}, "not a dict"]
    _sync_scheduled_actions_from_gallery(
        manifest,
        {"scheduled_actions": [{"id": "ok", "mechanism": "launchd"}, 42, None]},
    )
    assert len(manifest.scheduled_actions) == 1
    assert manifest.scheduled_actions[0]["id"] == "ok"


# ── _build_synthetic_job ─────────────────────────────────────────────────────


def test_synthetic_job_prefix_is_distinguishable() -> None:
    """The job_id must announce itself as an apply-actions invocation so
    audit-log readers don't mistake it for a real forge run."""
    job = _build_synthetic_job("atlas", "atlas-daily-digest", "p-7b26ba5e")
    assert job.job_id.startswith("apply-actions-")
    assert job.run_id.startswith("r-apply-")
    assert job.job_type == "apply"
    assert job.bot_id == "atlas"
    assert job.app_id == "atlas-daily-digest"
    assert job.pkg_id == "p-7b26ba5e"


# ── End-to-end happy path with mocked installers ────────────────────────────


def test_apply_actions_materializes_and_saves_stamps(
    shared_dir: Path, bot_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full happy path: manifest exists with a launchd action, the install
    helper is mocked, Phase 4.5 stamps the action, save_manifest persists
    the stamps. Re-running on the saved manifest is the idempotent case
    (the second invocation sees ``installed_by`` starting with ``forge:``
    on the action and skips)."""
    _write_manifest(
        bot_workspace,
        "atlas-daily-digest",
        pkg_id="p-7b26ba5e",
        scheduled_actions=[
            {
                "id":        "atlas-daily-digest",
                "mechanism": "launchd",
                "install":   {
                    "plist_label": "ai.evolve.${bot_id}.atlas-daily-digest",
                    "command":     "/bin/bash ${workspace}/scripts/atlas-digest-cron.sh",
                    "schedule":    {"cron": {"Hour": 7, "Minute": 0}},
                    "cwd":         "${workspace}",
                },
            },
        ],
    )
    _patch_workspace_resolution(monkeypatch, bot_workspace)

    # Mock the install helper so we don't shell out to launchctl.
    fake_install = mock.MagicMock(return_value={
        "ok":       True,
        "artifact": "/Library/LaunchDaemons/ai.evolve.atlas.atlas-daily-digest.plist",
        "error":    "",
        "loaded":   True,
    })
    monkeypatch.setattr(
        "evolve_admin.applications.install_helpers.install_launchd_command_action",
        fake_install,
    )

    result = apply_actions(
        "atlas", "atlas-daily-digest", shared_dir,
        network={"bots": {"atlas": {"user": "atlas"}}},
    )

    assert result.ok_count == 1
    assert result.failed_count == 0
    assert result.summary[0]["status"] == "ok"
    fake_install.assert_called_once()

    # Re-read the manifest from disk and confirm the installed_* stamps
    # landed. Without these, a second apply-actions run would re-install
    # rather than skip.
    on_disk = json.loads((bot_workspace / "atlas-daily-digest.json").read_text())
    action = on_disk["scheduled_actions"][0]
    assert action.get("installed_by", "").startswith("forge:apply-actions-")
    assert action.get("installed_artifact", "").endswith(".plist")
    assert action.get("installed_at")


def test_apply_actions_is_idempotent_on_second_run(
    shared_dir: Path, bot_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An action already stamped with installed_by=forge:* is skipped
    by Phase 4.5 without calling the installer at all."""
    _write_manifest(
        bot_workspace,
        "atlas-daily-digest",
        pkg_id="p-7b26ba5e",
        scheduled_actions=[
            {
                "id":                 "atlas-daily-digest",
                "mechanism":          "launchd",
                "install":            {
                    "plist_label": "ai.evolve.${bot_id}.atlas-daily-digest",
                    "command":     "/bin/bash run.sh",
                    "schedule":    {"every_minutes": 5},
                },
                "installed_by":       "forge:apply-actions-20260601T000000Z",
                "installed_at":       "2026-06-01T00:00:00Z",
                "installed_artifact": "/Library/LaunchDaemons/ai.evolve.atlas.atlas-daily-digest.plist",
            },
        ],
    )
    _patch_workspace_resolution(monkeypatch, bot_workspace)

    fake_install = mock.MagicMock(return_value={"ok": True})
    monkeypatch.setattr(
        "evolve_admin.applications.install_helpers.install_launchd_command_action",
        fake_install,
    )

    result = apply_actions(
        "atlas", "atlas-daily-digest", shared_dir,
        network={"bots": {"atlas": {"user": "atlas"}}},
    )

    # No installer call — Phase 4.5 saw the stamp and skipped.
    fake_install.assert_not_called()
    assert result.skipped_count == 1
    assert result.ok_count == 0
    assert result.failed_count == 0


def test_apply_actions_result_to_dict_shape() -> None:
    """The ``--json`` output of the CLI relies on to_dict's shape; pin it
    so a refactor can't silently rename fields."""
    r = ApplyActionsResult(
        bot_id="atlas", app_id="atlas-daily-digest", pkg_id="p-7b26ba5e",
        summary=[
            {"action_id": "a", "mechanism": "launchd", "status": "ok"},
            {"action_id": "b", "mechanism": "launchd", "status": "failed", "error": "x"},
            {"action_id": "c", "mechanism": "launchd", "status": "skipped"},
        ],
        synced_from_gallery=True,
    )
    d = r.to_dict()
    assert d["bot_id"] == "atlas"
    assert d["app_id"] == "atlas-daily-digest"
    assert d["pkg_id"] == "p-7b26ba5e"
    assert d["synced_from_gallery"] is True
    assert d["counts"] == {"ok": 1, "skipped": 1, "failed": 1}
    assert len(d["summary"]) == 3
