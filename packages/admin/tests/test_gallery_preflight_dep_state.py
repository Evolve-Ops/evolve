"""tests/test_gallery_preflight_dep_state.py

Coverage for the ``_installed_state`` helper inside
``gallery.preflight_check`` that drives the ``app_dependencies`` block
of the install modal's requirements check.

Bug history: pre-2026-06-01, ``_installed_state`` only treated
``manifest.status == "active"`` as "installed". But
``forge_engine._apply_forge_output`` writes ``status = "approved"`` on
successful install (and no later code path transitions that to
``"active"``). So a freshly-installed dependency app showed up as
``"installing"`` to every downstream install — blocking dependent apps
("Task Manager is currently being installed. Approve that forge job
first, then install this app.") until the operator manually flipped
the manifest status. Reproduced on a real install of EA Pack onto a
bot whose Task Manager install had just completed.

The fix accepts both ``"active"`` and ``"approved"`` as
post-install statuses. This file pins the canonical post-install
state, the in-progress states, the failed state, and the
no-manifest state against regression.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest


_ADMIN = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN))


# ─── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def shared_dir(tmp_path):
    return tmp_path / "shared"


def _dep_pkg(dep_pkg_id: str = "p-dep") -> dict:
    """A gallery package that requires ``dep_pkg_id`` as a hard
    dependency. Triggers the ``_installed_state(dep_pkg_id)`` lookup
    in ``preflight_check``."""
    return {
        "pkg_id": "test-pkg",
        "app_dependencies": [{
            "pkg_id": dep_pkg_id,
            "display_name": "Test Dependency",
            "required": True,
            "reason": "needs it",
        }],
        # No integration / secret / system requirements — keep the
        # preflight focused on the app-dependency block.
        "requirements": {},
    }


def _run_preflight(
    bot_id: str,
    dep_pkg_id: str,
    shared_dir: Path,
    *,
    manifest_dict: dict | None = None,
):
    """Invoke preflight_check, stubbing ``list_manifests`` so the
    dependency-state check sees exactly the manifest we want.

    ``manifest_dict`` is a plain dict; we turn it into an
    ``ApplicationManifest`` so ``m.pkg_id`` / ``m.status`` /
    ``m.install_job`` resolve correctly inside ``_installed_state``.
    """
    from evolve_admin.applications import gallery
    from evolve_admin.applications.manifest import ApplicationManifest

    manifests: list[ApplicationManifest] = []
    if manifest_dict is not None:
        manifests.append(ApplicationManifest.from_dict(manifest_dict))

    with patch.object(gallery, "load_gallery_package",
                      return_value=_dep_pkg(dep_pkg_id)), \
         patch("evolve_admin.applications.manifest.list_manifests",
               return_value=manifests), \
         patch("evolve_admin.config.load_network",
               return_value={"bots": {bot_id: {"role": "member"}}}):
        return gallery.preflight_check("test-pkg", bot_id, shared_dir)


def _dep_block(preflight: dict) -> dict:
    """Pull the single app_dependencies entry out of a preflight result."""
    deps = preflight["app_dependencies"]
    assert len(deps) == 1, f"expected 1 dep block, got {len(deps)}: {deps!r}"
    return deps[0]


# ─── installed states ────────────────────────────────────────────────────────


def test_dep_installed_when_status_approved(shared_dir):
    """REGRESSION 2026-06-01: post-install status is "approved" (set by
    forge_engine._apply_forge_output), not "active". The dependency
    check must treat both as installed; before the fix it only
    accepted "active" and a freshly-installed Task Manager blocked
    every dependent install."""
    result = _run_preflight(
        bot_id="bot-a",
        dep_pkg_id="p-dep",
        shared_dir=shared_dir,
        manifest_dict={
            "id": "task-manager",
            "name": "task-manager",
            "bot_id": "bot-a",
            "pkg_id": "p-dep",
            "status": "approved",
            "install_job": None,
        },
    )
    dep = _dep_block(result)
    assert dep["state"] == "installed", dep
    assert dep["severity"] == "info"
    assert "installed" in dep["message"].lower()


def test_dep_installed_when_status_active(shared_dir):
    """The pre-existing "active" branch still resolves to installed —
    pinned so a future refactor of _installed_state doesn't drop it."""
    result = _run_preflight(
        bot_id="bot-a",
        dep_pkg_id="p-dep",
        shared_dir=shared_dir,
        manifest_dict={
            "id": "task-manager",
            "name": "task-manager",
            "bot_id": "bot-a",
            "pkg_id": "p-dep",
            "status": "active",
            "install_job": None,
        },
    )
    dep = _dep_block(result)
    assert dep["state"] == "installed", dep


# ─── in-progress states ──────────────────────────────────────────────────────


def test_dep_installing_when_install_job_awaiting_approval(shared_dir):
    """During the operator-approval window, install_job is a dict with
    phase=awaiting_approval. Dependency check must report installing
    (not installed) so the dependent install waits for approval."""
    result = _run_preflight(
        bot_id="bot-a",
        dep_pkg_id="p-dep",
        shared_dir=shared_dir,
        manifest_dict={
            "id": "task-manager",
            "name": "task-manager",
            "bot_id": "bot-a",
            "pkg_id": "p-dep",
            "status": "updating",
            "install_job": {
                "job_id": "j-abc",
                "status": "awaiting_approval",
            },
        },
    )
    dep = _dep_block(result)
    assert dep["state"] == "installing", dep
    assert dep["severity"] == "build_blocker"
    assert "currently being installed" in dep["message"].lower()


def test_dep_installing_when_status_updating_no_job_dict(shared_dir):
    """Between Step 1 (seed) and Step 8 (mark_awaiting_approval), the
    install_job field is just a job_id string (or None) and the
    manifest status is "updating". The check must still classify this
    as installing, not as installed."""
    result = _run_preflight(
        bot_id="bot-a",
        dep_pkg_id="p-dep",
        shared_dir=shared_dir,
        manifest_dict={
            "id": "task-manager",
            "name": "task-manager",
            "bot_id": "bot-a",
            "pkg_id": "p-dep",
            "status": "updating",
            "install_job": None,
        },
    )
    dep = _dep_block(result)
    assert dep["state"] == "installing", dep


# ─── failed state ────────────────────────────────────────────────────────────


def test_dep_failed_when_install_job_rejected(shared_dir):
    """A rejected install_job means the dependency was abandoned;
    surface that distinctly so the operator knows to reinstall."""
    result = _run_preflight(
        bot_id="bot-a",
        dep_pkg_id="p-dep",
        shared_dir=shared_dir,
        manifest_dict={
            "id": "task-manager",
            "name": "task-manager",
            "bot_id": "bot-a",
            "pkg_id": "p-dep",
            "status": "draft",
            "install_job": {"job_id": "j-abc", "status": "rejected"},
        },
    )
    dep = _dep_block(result)
    assert dep["state"] == "failed", dep
    assert "reinstall" in dep["message"].lower()


# ─── not-installed state ─────────────────────────────────────────────────────


def test_dep_not_installed_when_no_manifest_matches_pkg_id(shared_dir):
    """No manifest on the bot has the dependency's pkg_id → must
    classify as not_installed so the install modal tells the
    operator to install the dependency first."""
    result = _run_preflight(
        bot_id="bot-a",
        dep_pkg_id="p-dep",
        shared_dir=shared_dir,
        manifest_dict={
            "id": "some-other-app",
            "name": "some-other-app",
            "bot_id": "bot-a",
            "pkg_id": "p-different",
            "status": "approved",
            "install_job": None,
        },
    )
    dep = _dep_block(result)
    assert dep["state"] == "not_installed", dep
    assert "must be installed first" in dep["message"].lower()


def test_dep_not_installed_when_no_manifests_at_all(shared_dir):
    """No manifests on the bot at all → also not_installed."""
    result = _run_preflight(
        bot_id="bot-a",
        dep_pkg_id="p-dep",
        shared_dir=shared_dir,
        manifest_dict=None,
    )
    dep = _dep_block(result)
    assert dep["state"] == "not_installed", dep
