"""tests/test_forge_rebuild_buildspec.py — rebuild build_spec sourcing.

A v7-arc Instance manifest carries no ``build_spec`` — the gallery
package's build_spec lives in the bound Spec and is snapshotted into the
forge job's ``context_snapshot`` at install time (the gallery install
route and the pack_driver both do this). ``assemble_context_package``
must therefore fall back to that snapshot when an *existing* instance
manifest has no build_spec of its own; otherwise a re-build re-forges
from an empty spec and the bot keeps its prior (possibly stale) files.

That gap is exactly why re-forging a v7-arc app could not pick up a
corrected Spec — e.g. the OC-2026.6 ``/api/message`` → ``openclaw
message send`` gallery migration: a re-install left the dead endpoint in
place because the build LLM never saw the corrected spec. Spec trail:
internal/decision-add-bot-m4-u1-proof-2026-06-11.md (§Delivery evidence).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for _p in (str(_ADMIN), str(_ANALYZER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evolve_admin.applications import forge_engine
from evolve_admin.applications.forge_jobs import ForgeJob, _install_steps

_PKG_BUILD_SPEC = "## Build Specification\n\nDeliver via `openclaw message send`."


def _job(context_snapshot=None):
    return ForgeJob(
        job_id="j-test1234",
        run_id="r-00000001",
        job_type="install",
        pkg_id="p-test1234",
        app_id="morning-briefing",
        bot_id="test-bot",
        pkg_version_before=None,
        gallery_version=None,
        steps=_install_steps(),
        status="queued",
        context_snapshot=context_snapshot or {},
    )


class _FakeManifest:
    """A v7-arc Instance: realized, but carries no build_spec of its own."""

    def __init__(self, build_spec: str = ""):
        self.build_spec = build_spec
        self.improvement_history: list = []
        self.pkg_id = "p-test1234"

    def to_dict(self) -> dict:
        return {
            "id": "morning-briefing",
            "bot_id": "test-bot",
            "pkg_id": "p-test1234",
            "status": "active",
        }


def test_rebuild_falls_back_to_snapshot_build_spec_when_instance_has_none(tmp_path):
    """v7-arc rebuild (manifest exists, no instance build_spec) must take the
    package build_spec snapshotted into context_snapshot — not an empty
    string. Regression: re-forge could not pick up a corrected Spec."""
    job = _job({"build_spec": _PKG_BUILD_SPEC})
    with patch.object(forge_engine, "load_manifest",
                      return_value=_FakeManifest(build_spec="")):
        ctx = forge_engine.assemble_context_package(job, tmp_path)
    assert ctx["build_spec"] == _PKG_BUILD_SPEC


def test_rebuild_prefers_instance_build_spec_when_present(tmp_path):
    """A v6 manifest that carries its own build_spec keeps it — the snapshot
    fallback only fills the v7-arc empty-build_spec gap, never overrides a
    real instance spec."""
    job = _job({"build_spec": _PKG_BUILD_SPEC})
    with patch.object(forge_engine, "load_manifest",
                      return_value=_FakeManifest(build_spec="v6 instance spec")):
        ctx = forge_engine.assemble_context_package(job, tmp_path)
    assert ctx["build_spec"] == "v6 instance spec"


def test_fresh_install_uses_snapshot_build_spec(tmp_path):
    """No manifest yet → the first-install path already used
    context_snapshot; guard it stays that way."""
    job = _job({"build_spec": _PKG_BUILD_SPEC})
    with patch.object(forge_engine, "load_manifest", return_value=None):
        ctx = forge_engine.assemble_context_package(job, tmp_path)
    assert ctx["build_spec"] == _PKG_BUILD_SPEC
