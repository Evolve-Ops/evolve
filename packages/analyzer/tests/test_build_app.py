"""tests/test_build_app.py — BuildApp action, applier, and forge sweep.

Tests use injected stand-ins for the forge primitives (kickoff runner +
job loader) so they don't depend on the heavy ``forge_engine`` /
``forge_jobs`` modules being importable in the analyzer test env.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter import apply as arbiter_apply  # noqa: E402
from arbiter.appliers import build_app  # noqa: E402
from arbiter.forge_sweep import sweep  # noqa: E402
from arbiter.state_machine import transition  # noqa: E402
from arbiter.store import iter_proposals, write_proposal  # noqa: E402
from schema.proposal import (  # noqa: E402
    BuildApp,
    Proposal,
    Provenance,
    RiskTag,
    new_proposal_id,
)


# ──────────────────────────────────────────────────────────────────────────
# Fixture helpers
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class _FakeForgeJob:
    """Stand-in for ``forge_jobs.ForgeJob`` in tests. The applier and
    sweep only read ``status`` / ``job_id`` so we don't need the full
    dataclass shape."""

    job_id: str
    status: str = "queued"
    bot_id: str = ""
    app_id: str = ""


def _make_buildapp_proposal(
    bot_id: str = "team_bot_a",
    app_id: str = "macros-tracker",
    manifest: dict | None = None,
) -> Proposal:
    if manifest is None:
        manifest = {
            "id": app_id,
            "name": "Macros Tracker",
            "bot_id": bot_id,
            "behaviors": ["Track daily macros."],
        }
    return Proposal(
        id=new_proposal_id(),
        bot_id=bot_id,
        generator_id="hypothetical_app_advisor",
        dimension="capability_growth",
        trigger_observations=[f"app_proposal:{bot_id}:{app_id}"],
        provenance=Provenance(technique="test"),
        problem=f"Build {app_id} for {bot_id}",
        action=BuildApp(
            bot_id=bot_id,
            app_id=app_id,
            app_name="Macros Tracker",
            manifest=manifest,
        ),
        risk_tag=RiskTag(blast_radius="bot", reversibility="manual", touches=["app_install"]),
        approval_audience="pod_operator",
        urgency="improvement",
        admin_surface_summary=f"build {app_id}",
    )


# ──────────────────────────────────────────────────────────────────────────
# Schema
# ──────────────────────────────────────────────────────────────────────────


def test_buildapp_action_roundtrips_through_proposal_dict():
    p = _make_buildapp_proposal()
    blob = p.to_dict()
    p2 = Proposal.from_dict(blob)
    assert isinstance(p2.action, BuildApp)
    assert p2.action.app_id == "macros-tracker"
    assert p2.action.manifest["name"] == "Macros Tracker"


def test_buildapp_kind_is_in_action_registry():
    from schema.proposal import _ACTION_KIND_REGISTRY

    assert _ACTION_KIND_REGISTRY["BuildApp"] is BuildApp


# ──────────────────────────────────────────────────────────────────────────
# Applier
# ──────────────────────────────────────────────────────────────────────────


def test_buildapp_applier_writes_manifest_and_kicks_off_forge(tmp_path: Path, monkeypatch):
    build_app.set_shared_dir(tmp_path)

    # Capture the kickoff invocation so we know forge would have run.
    invocations: list[tuple] = []

    def fake_runner(shared_dir, job_id, bot_id):
        invocations.append((shared_dir, job_id, bot_id))

    build_app.set_kickoff_runner(fake_runner)

    # Stub out ForgeJob creation since the test env doesn't have
    # evolve_admin.applications. The applier calls _create_forge_job which
    # imports forge_jobs lazily — patch the module-level helper directly.
    fake_job = _FakeForgeJob(job_id="j-test-001", bot_id="team_bot_a", app_id="macros-tracker")

    def fake_create_job(*, pkg_id, app_id, bot_id, shared_dir):
        return fake_job

    monkeypatch.setattr(build_app, "_create_forge_job", fake_create_job)

    p = _make_buildapp_proposal()
    transition(p, "pending", actor="t")
    transition(p, "approved_human", actor="user")

    outcome = arbiter_apply.apply(p, actor="user")

    try:
        assert outcome.ok
        assert outcome.result.details["job_id"] == "j-test-001"
        assert outcome.result.details["app_id"] == "macros-tracker"

        # Manifest written to disk
        manifest_path = tmp_path / "applications" / "team_bot_a" / "macros-tracker.json"
        assert manifest_path.exists()
        loaded = json.loads(manifest_path.read_text())
        assert loaded["name"] == "Macros Tracker"

        # Job_id captured in provenance.signals._apply_details so the
        # forge sweep can find it later.
        assert (
            p.provenance.signals.get("_apply_details", {}).get("job_id")
            == "j-test-001"
        )

        # Forge runner was kicked off (in the test it ran sync via the
        # injected callable; in production it runs in a daemon thread).
        # The applier wraps the runner in threading.Thread.start(), so
        # we wait briefly for the thread to invoke our fake.
        import time

        deadline = time.time() + 2.0
        while not invocations and time.time() < deadline:
            time.sleep(0.01)
        assert len(invocations) == 1
        _shared, job_id, bot = invocations[0]
        assert job_id == "j-test-001"
        assert bot == "team_bot_a"

        # Apply lifecycle: BuildApp has a claim-less proposal but is NOT
        # a manual-completion kind, so apply.py should auto-promote to
        # succeeded? No — BuildApp shouldn't auto-succeed because the
        # forge sweep owns that transition. Verify the proposal lands in
        # ``applied``.
        assert p.status == "applied"
    finally:
        build_app.set_kickoff_runner(None)
        build_app.set_shared_dir(Path("/Users/Shared/evolve"))


def test_buildapp_applier_rejects_empty_manifest(tmp_path: Path):
    build_app.set_shared_dir(tmp_path)
    try:
        action = BuildApp(
            bot_id="team_bot_a",
            app_id="empty",
            app_name="Empty",
            manifest={},
        )
        applier = build_app.BuildAppApplier()
        result = applier.apply(action, "team_bot_a")
        assert not result.ok
        assert "empty" in result.message.lower()
    finally:
        build_app.set_shared_dir(Path("/Users/Shared/evolve"))


def test_buildapp_applier_refuses_to_overwrite_existing_manifest(
    tmp_path: Path, monkeypatch
):
    """Second BuildApp for the same app_id must fail cleanly without
    silently clobbering the prior manifest, and route the proposal to
    failed_flagged for operator visibility."""
    build_app.set_shared_dir(tmp_path)

    # Stub the forge runner + job creation so the first apply doesn't
    # try to import forge_engine.
    build_app.set_kickoff_runner(lambda *a, **kw: None)
    fake_job = _FakeForgeJob(
        job_id="j-conflict-001", bot_id="team_bot_a", app_id="macros-tracker"
    )
    monkeypatch.setattr(
        build_app,
        "_create_forge_job",
        lambda *, pkg_id, app_id, bot_id, shared_dir: fake_job,
    )

    try:
        # First apply — succeeds, lands manifest on disk.
        first = _make_buildapp_proposal(
            manifest={
                "id": "macros-tracker",
                "name": "Macros v1",
                "bot_id": "team_bot_a",
            },
        )
        transition(first, "pending", actor="t")
        transition(first, "approved_human", actor="user")
        outcome1 = arbiter_apply.apply(first, actor="user")
        assert outcome1.ok
        manifest_path = tmp_path / "applications" / "team_bot_a" / "macros-tracker.json"
        assert manifest_path.exists()
        original = json.loads(manifest_path.read_text())
        assert original["name"] == "Macros v1"

        # Second apply with the SAME app_id — must refuse, must not
        # overwrite, and must land the proposal in failed_flagged.
        second = _make_buildapp_proposal(
            manifest={
                "id": "macros-tracker",
                "name": "Macros v2 OVERWRITE",
                "bot_id": "team_bot_a",
            },
        )
        transition(second, "pending", actor="t")
        transition(second, "approved_human", actor="user")
        outcome2 = arbiter_apply.apply(second, actor="user")

        assert not outcome2.ok
        assert "already exists" in outcome2.result.message.lower()
        assert outcome2.result.details.get("fail_action") == "flag"
        assert second.status == "failed_flagged"

        # Original manifest content preserved.
        preserved = json.loads(manifest_path.read_text())
        assert preserved["name"] == "Macros v1"
    finally:
        build_app.set_kickoff_runner(None)
        build_app.set_shared_dir(Path("/Users/Shared/evolve"))


def test_buildapp_applier_rejects_bot_id_mismatch(tmp_path: Path):
    build_app.set_shared_dir(tmp_path)
    try:
        action = BuildApp(
            bot_id="team_bot_a",
            app_id="m",
            app_name="M",
            manifest={"id": "m"},
        )
        applier = build_app.BuildAppApplier()
        # Caller passes a different bot_id than the action carries
        result = applier.apply(action, "team_bot_b")
        assert not result.ok
        assert "does not match" in result.message
    finally:
        build_app.set_shared_dir(Path("/Users/Shared/evolve"))


# ──────────────────────────────────────────────────────────────────────────
# Forge sweep
# ──────────────────────────────────────────────────────────────────────────


def _persist_buildapp_in_applied(tmp_path: Path, *, job_id: str = "j-001") -> Proposal:
    p = _make_buildapp_proposal()
    transition(p, "pending", actor="t")
    transition(p, "approved_human", actor="user")
    transition(p, "applied", actor="user")
    p.provenance.signals["_apply_details"] = {"job_id": job_id}
    write_proposal(p, tmp_path, subdir="applied")
    return p


def test_forge_sweep_promotes_complete_job_to_succeeded(tmp_path: Path):
    p = _persist_buildapp_in_applied(tmp_path, job_id="j-A")

    def loader(job_id, shared_dir):
        return _FakeForgeJob(job_id=job_id, status="complete")

    counts = sweep(tmp_path, job_loader=loader)
    assert counts["succeeded"] == 1
    assert counts["failed"] == 0

    # File moved out of applied/, no longer present
    assert not (tmp_path / "proposals" / "applied" / f"{p.id}.json").exists()
    # Lands in archived/ (= where succeeded routes per the store map)
    assert (tmp_path / "proposals" / "archived" / f"{p.id}.json").exists()


def test_forge_sweep_marks_failed_job_as_failed_flagged(tmp_path: Path):
    p = _persist_buildapp_in_applied(tmp_path, job_id="j-B")

    def loader(job_id, shared_dir):
        return _FakeForgeJob(job_id=job_id, status="failed")

    counts = sweep(tmp_path, job_loader=loader)
    assert counts["failed"] == 1
    assert counts["succeeded"] == 0

    # Reload from archived; status should be failed_flagged
    archived = list(iter_proposals(tmp_path, subdirs=("archived",)))
    assert len(archived) == 1
    assert archived[0].status == "failed_flagged"


def test_forge_sweep_leaves_in_flight_job_alone(tmp_path: Path):
    p = _persist_buildapp_in_applied(tmp_path, job_id="j-C")

    def loader(job_id, shared_dir):
        return _FakeForgeJob(job_id=job_id, status="running")

    counts = sweep(tmp_path, job_loader=loader)
    assert counts["in_flight"] == 1
    assert counts["succeeded"] == 0
    assert counts["failed"] == 0

    # File stays put
    assert (tmp_path / "proposals" / "applied" / f"{p.id}.json").exists()


def test_forge_sweep_handles_missing_job_id_gracefully(tmp_path: Path):
    p = _make_buildapp_proposal()
    transition(p, "pending", actor="t")
    transition(p, "approved_human", actor="user")
    transition(p, "applied", actor="user")
    # No _apply_details written — proposal got into applied/ via some
    # path that didn't run apply.py (test scaffolding, partial state).
    write_proposal(p, tmp_path, subdir="applied")

    counts = sweep(tmp_path, job_loader=lambda *a: None)
    assert counts["missing"] == 1
    # Proposal stays in applied/ — we don't speculate on its fate
    assert (tmp_path / "proposals" / "applied" / f"{p.id}.json").exists()


def test_forge_sweep_skips_non_buildapp_proposals(tmp_path: Path):
    """A ConfigPatch sitting in applied/ shouldn't be touched by this sweep."""
    from testing.harness import make_config_patch_proposal

    target = tmp_path / "cfg.json"
    target.write_text(json.dumps({"k": "v"}))
    p = make_config_patch_proposal(target_path=f"{target}::k", value="dark")
    transition(p, "pending", actor="t")
    transition(p, "approved_auto", actor="t")
    transition(p, "applied", actor="t")
    write_proposal(p, tmp_path, subdir="applied")

    # Loader would explode — we want to verify it's never called for
    # non-BuildApp proposals.
    def boom(job_id, shared_dir):
        raise AssertionError("loader should not be called for ConfigPatch")

    counts = sweep(tmp_path, job_loader=boom, stale_sweeper=lambda _: [])
    assert counts == {
        "succeeded": 0,
        "failed": 0,
        "in_flight": 0,
        "missing": 0,
        "auto_rejected": 0,
        "pruned": 0,
        # 2026-06-05: orphan-step janitor runs inside the sweep cycle;
        # with no active forge jobs on disk it's a clean no-op.
        "orphan_jobs_reconciled": 0,
        "orphan_steps_reconciled": 0,
    }
    # Proposal stays put
    assert (tmp_path / "proposals" / "applied" / f"{p.id}.json").exists()


def test_forge_sweep_invokes_stale_sweeper(tmp_path: Path):
    """The sweep delegates stale-job auto-reject to the stale_sweeper
    callable and surfaces the count for cycle logging."""
    invoked: list[Path] = []

    def fake_stale(shared_dir: Path) -> list[str]:
        invoked.append(shared_dir)
        return ["j-stale-1", "j-stale-2"]

    counts = sweep(
        tmp_path,
        job_loader=lambda *a: None,
        stale_sweeper=fake_stale,
    )
    assert invoked == [tmp_path]
    assert counts["auto_rejected"] == 2


def test_forge_sweep_tolerates_stale_sweeper_exception(tmp_path: Path):
    """A crash in the stale-sweeper must not break the proposal pass."""
    p = _persist_buildapp_in_applied(tmp_path, job_id="j-tolerate")

    def boom(_shared):
        raise RuntimeError("forge_jobs unimportable, hypothetically")

    def loader(job_id, shared_dir):
        return _FakeForgeJob(job_id=job_id, status="complete")

    counts = sweep(tmp_path, job_loader=loader, stale_sweeper=boom)
    # Proposal pass still ran; auto_rejected stays at 0.
    assert counts["succeeded"] == 1
    assert counts["auto_rejected"] == 0


# ──────────────────────────────────────────────────────────────────────────
# Daemon-thread crash recovery (fix #4)
# ──────────────────────────────────────────────────────────────────────────


def test_buildapp_runner_marks_job_failed_when_forge_crashes(
    tmp_path: Path, monkeypatch
):
    """If the forge daemon thread raises, the kickoff runner must mark
    the forge job failed so forge_sweep can transition the proposal to
    failed_flagged. Without this, the job sits in 'queued'/'running'
    forever and the proposal stays in 'applied'."""
    # The applier's _default_kickoff_runner imports forge_engine lazily
    # and run_forge_job is the call that may raise. We stub
    # forge_engine.run_forge_job to raise, and assert the job's state
    # gets flipped to 'failed' afterwards.
    from evolve_admin.applications import forge_jobs as _fj  # type: ignore
    from evolve_admin.applications import forge_engine as _fe  # type: ignore

    job = _fj.ForgeJob(
        job_id="j-crash-001",
        run_id="r-00000001",
        job_type="install",
        pkg_id="chat-deadbeef",
        app_id="crashy",
        bot_id="team_bot_a",
        pkg_version_before=None,
        gallery_version=None,
        steps=_fj._install_steps(),
        created_at=_fj.now_iso(),
        last_updated=_fj.now_iso(),
    )
    _fj.save_job(job, tmp_path)
    _fj.mark_step_running(job, 2, tmp_path)

    def explode(**_kwargs):
        raise RuntimeError("forge LLM blew up mid-step")

    monkeypatch.setattr(_fe, "run_forge_job", explode)

    build_app._default_kickoff_runner(tmp_path, "j-crash-001", "team_bot_a")

    reloaded = _fj.load_job("j-crash-001", tmp_path)
    assert reloaded is not None
    assert reloaded.status == "failed"
    # Step 2 (the running one) should carry the crash detail.
    step2 = next(s for s in reloaded.steps if s.num == 2)
    assert step2.status == "failed"
    assert "forge LLM blew up" in step2.detail
