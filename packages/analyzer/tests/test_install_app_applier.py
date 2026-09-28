"""tests/test_install_app_applier.py — the InstallApp applier + its completion path.

Registration is the cheap half. What actually has to hold for an approved
Fit Reviewer proposal to install anything is a chain of four things, each
covered here:

  1. the kind resolves to an applier at all (the shipped failure: none did),
  2. apply creates a real forge install job and dispatches it,
  3. apply.py leaves the proposal in ``applied`` instead of declaring
     success before forge has run (``_EXTERNAL_COMPLETION_KINDS``),
  4. ``forge_sweep`` closes it out from the job's terminal status.

Break any one and the proposal either fails loudly or lies quietly. The
admin ``applications`` package is not importable from the analyzer test env,
so the gallery/forge_jobs seam is faked at ``sys.modules`` — the applier
reaches it by lazy import, which is exactly the shape production uses.
"""

from __future__ import annotations

import subprocess
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter import forge_sweep  # noqa: E402
from arbiter.apply import (  # noqa: E402
    is_external_completion_kind,
    _BREAKER_EXEMPT_KINDS,
)
from arbiter.appliers import get_applier, known_action_kinds  # noqa: E402
from schema.proposal import InstallApp  # noqa: E402

BOT = "team_bot_a"
PKG = "p-9bfa1c84"


# ─────────────────────────────────────────────────────────────────────────────
# Fakes for the admin applications seam
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _FakeJob:
    job_id: str = "j-test-0001"
    status: str = "queued"
    context_snapshot: dict = field(default_factory=dict)


class _Recorder:
    def __init__(self):
        self.created: list[dict] = []
        self.saved: list[_FakeJob] = []
        self.dispatched: list[tuple] = []


@pytest.fixture()
def seam(monkeypatch, tmp_path):
    """Install fake ``evolve_admin.applications.*`` modules + a fake runner."""
    from arbiter.appliers import install_app

    rec = _Recorder()
    job = _FakeJob()

    state = {
        "pkg": {"name": "Task Manager", "pkg_version": "1.2.0", "build_spec": "SPEC"},
        "installed_state": "not_installed",
        "preflight": {"app_dependencies": [], "requirements": []},
    }

    gallery = types.ModuleType("evolve_admin.applications.gallery")
    gallery.load_gallery_package = lambda pkg_id, shared: state["pkg"]  # type: ignore[attr-defined]
    gallery.installed_state = lambda pkg_id, bot, shared: state["installed_state"]  # type: ignore[attr-defined]
    gallery.preflight_check = lambda pkg_id, bot, shared: state["preflight"]  # type: ignore[attr-defined]

    def _create_install_job(*, pkg_id, app_id, bot_id, gallery_version, shared_dir):
        rec.created.append({
            "pkg_id": pkg_id, "app_id": app_id, "bot_id": bot_id,
            "gallery_version": gallery_version, "shared_dir": shared_dir,
        })
        return job

    forge_jobs = types.ModuleType("evolve_admin.applications.forge_jobs")
    forge_jobs.create_install_job = _create_install_job  # type: ignore[attr-defined]
    forge_jobs.save_job = lambda j, shared: rec.saved.append(j)  # type: ignore[attr-defined]

    for name, mod in (
        ("evolve_admin.applications.gallery", gallery),
        ("evolve_admin.applications.forge_jobs", forge_jobs),
    ):
        monkeypatch.setitem(sys.modules, name, mod)

    install_app.set_shared_dir(tmp_path)
    monkeypatch.setattr(
        install_app,
        "_kickoff_runner",
        lambda shared, job_id, bot_id: rec.dispatched.append((shared, job_id, bot_id)),
    )
    yield types.SimpleNamespace(rec=rec, job=job, state=state, shared=tmp_path)
    install_app.set_shared_dir(Path("/Users/Shared/evolve"))


def _action(pkg_id: str = PKG, source: str = "gallery") -> InstallApp:
    return InstallApp(app_id=pkg_id, source=source)


# ─────────────────────────────────────────────────────────────────────────────
# Registration
# ─────────────────────────────────────────────────────────────────────────────


def test_applier_is_registered():
    assert get_applier("InstallApp") is not None
    assert "InstallApp" in known_action_kinds()


def test_registered_by_the_package_import_alone():
    """The shipped failure was "no applier registered for kind 'InstallApp'".

    Any direct import of ``arbiter.appliers.install_app`` in this file would
    register the kind as a side effect, so a fresh interpreter that imports
    only the package is the check that actually bites the ``__init__`` line.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, %r);\n"
            "from arbiter.appliers import known_action_kinds;\n"
            "print('InstallApp' in known_action_kinds())" % str(_ANALYZER_DIR),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "True", proc.stdout + proc.stderr


# ─────────────────────────────────────────────────────────────────────────────
# apply — the happy path creates and dispatches a real job
# ─────────────────────────────────────────────────────────────────────────────


def test_apply_creates_and_dispatches_forge_job(seam):
    result = get_applier("InstallApp").apply(_action(), BOT)
    assert result.ok, result.message
    assert len(seam.rec.created) == 1
    created = seam.rec.created[0]
    assert created["pkg_id"] == PKG
    assert created["bot_id"] == BOT
    assert created["gallery_version"] == "1.2.0"
    # app_id is the package-name slug, not the pkg_id — the same spelling the
    # UI / evo install / action.app.install all derive.
    assert created["app_id"] == "task-manager"
    assert result.details["job_id"] == seam.job.job_id
    assert seam.rec.dispatched == [(seam.shared, seam.job.job_id, BOT)]


def test_apply_carries_build_spec_onto_the_job(seam):
    assert get_applier("InstallApp").apply(_action(), BOT).ok
    assert seam.job.context_snapshot["build_spec"] == "SPEC"
    assert seam.rec.saved == [seam.job]  # re-saved after stashing the spec


def test_apply_without_build_spec_does_not_resave(seam):
    seam.state["pkg"] = {"name": "Task Manager", "pkg_version": "1.2.0"}
    assert get_applier("InstallApp").apply(_action(), BOT).ok
    assert "build_spec" not in seam.job.context_snapshot
    assert seam.rec.saved == []


# ─────────────────────────────────────────────────────────────────────────────
# apply — refusals, all pre-side-effect
# ─────────────────────────────────────────────────────────────────────────────


def _assert_flagged_without_job(result, seam, needle: str):
    assert result.ok is False
    assert result.details.get("fail_action") == "flag"
    assert needle in result.message
    assert seam.rec.created == []
    assert seam.rec.dispatched == []


def test_non_gallery_source_is_refused(seam):
    _assert_flagged_without_job(
        get_applier("InstallApp").apply(_action(source="custom"), BOT),
        seam,
        "not installable",
    )


def test_empty_pkg_id_is_refused(seam):
    _assert_flagged_without_job(
        get_applier("InstallApp").apply(_action(pkg_id="  "), BOT), seam, "no app_id"
    )


def test_unknown_package_is_refused(seam):
    seam.state["pkg"] = None
    _assert_flagged_without_job(
        get_applier("InstallApp").apply(_action(), BOT), seam, "not found"
    )


@pytest.mark.parametrize("state", ["installed", "installing"])
def test_already_installed_is_refused(seam, state):
    seam.state["installed_state"] = state
    _assert_flagged_without_job(
        get_applier("InstallApp").apply(_action(), BOT), seam, "already has"
    )


def test_failed_prior_install_does_not_block_a_retry(seam):
    """``failed`` is not ``installed`` — a retry must be allowed through."""
    seam.state["installed_state"] = "failed"
    assert get_applier("InstallApp").apply(_action(), BOT).ok
    assert len(seam.rec.created) == 1


def test_build_blockers_are_refused(seam):
    seam.state["preflight"] = {
        "app_dependencies": [
            {"display_name": "EA Pack", "severity": "build_blocker",
             "state": "not_installed", "message": "install EA Pack first"}
        ],
        "requirements": [],
    }
    result = get_applier("InstallApp").apply(_action(), BOT)
    _assert_flagged_without_job(result, seam, "build blocker")
    assert "EA Pack" in result.message


def test_satisfied_blockers_do_not_refuse(seam):
    seam.state["preflight"] = {
        "app_dependencies": [
            {"display_name": "EA Pack", "severity": "build_blocker",
             "state": "satisfied"}
        ],
        "requirements": [
            {"type": "system", "severity": "runtime_warning", "state": "missing"}
        ],
    }
    assert get_applier("InstallApp").apply(_action(), BOT).ok


def test_missing_oauth_integration_is_refused(seam):
    """The OAuth flow is UI-only; a job parked in awaiting_oauth would never
    be resumed from behind a proposal, so refuse instead of queueing one."""
    seam.state["preflight"] = {
        "app_dependencies": [],
        "requirements": [
            {"type": "integration", "display_name": "Google (GOG)",
             "severity": "build_blocker", "state": "missing"}
        ],
    }
    result = get_applier("InstallApp").apply(_action(), BOT)
    _assert_flagged_without_job(result, seam, "integration")
    assert "Google (GOG)" in result.message


def test_admin_package_unavailable_is_not_flagged(monkeypatch, tmp_path):
    """An environment fault must not burn the proposal.

    A missing admin package means this host cannot apply; flagging would
    move the proposal to failed_flagged and require an operator to re-raise
    it. It stays a plain failure so a later sweep can retry.
    """
    from arbiter.appliers import install_app

    install_app.set_shared_dir(tmp_path)
    real_import = __import__

    def _boom(name, *args, **kwargs):
        if name.startswith("evolve_admin.applications"):
            raise ImportError("no admin package here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _boom)
    result = get_applier("InstallApp").apply(_action(), BOT)
    monkeypatch.undo()
    install_app.set_shared_dir(Path("/Users/Shared/evolve"))

    assert result.ok is False
    assert "fail_action" not in result.details
    assert result.details.get("reason") == "applications_unavailable"


# ─────────────────────────────────────────────────────────────────────────────
# snapshot / revert
# ─────────────────────────────────────────────────────────────────────────────


def test_snapshot_records_the_attempt(seam):
    snap = get_applier("InstallApp").capture_snapshot(_action(), BOT)
    assert snap["action_kind"] == "InstallApp"
    assert snap["pkg_id"] == PKG
    assert snap["bot_id"] == BOT
    assert seam.rec.created == []  # snapshot must not dispatch anything


def test_revert_reports_manual_rather_than_claiming_an_undo(seam):
    snap = get_applier("InstallApp").capture_snapshot(_action(), BOT)
    result = get_applier("InstallApp").revert(snap, BOT)
    assert result.ok is False  # honest: nothing was undone
    assert result.details["manual"] is True
    assert PKG in result.message
    assert "Forge Jobs" in result.message


# ─────────────────────────────────────────────────────────────────────────────
# The completion path — who owns applied → succeeded
# ─────────────────────────────────────────────────────────────────────────────


def test_install_app_is_an_external_completion_kind():
    """Without this, apply.py auto-succeeds the claim-less proposal on apply
    — declaring the install done before forge has started."""
    assert is_external_completion_kind("InstallApp")


def test_install_app_is_not_breaker_exempt():
    """External-completion and breaker-exempt are different questions.

    An install puts files (and sometimes plugin entries) on the bot, which is
    what a tripped config_change breaker exists to hold off. BuildApp stays
    exempt exactly as it shipped.
    """
    assert "InstallApp" not in _BREAKER_EXEMPT_KINDS
    assert "BuildApp" in _BREAKER_EXEMPT_KINDS
    assert "Investigation" in _BREAKER_EXEMPT_KINDS


@pytest.mark.parametrize(
    "job_status,expected",
    [("complete", "succeeded"), ("failed", "failed_flagged"),
     ("rejected", "failed_flagged")],
)
def test_forge_sweep_closes_out_installapp(tmp_path, job_status, expected):
    """The sweep must recognise InstallApp, not just BuildApp."""
    from arbiter import store as arbiter_store
    from arbiter.state_machine import transition
    from schema.proposal import Proposal, RiskTag, new_proposal_id
    from schema.provenance import Provenance

    proposal = Proposal(
        id=new_proposal_id(),
        bot_id=BOT,
        generator_id="fit_reviewer",
        dimension="capabilities",
        trigger_observations=["fit_review:x"],
        provenance=Provenance(
            technique="fit_reviewer.v1",
            signals={"_apply_details": {"job_id": "j-sweep-1"}},
            confidence=1.0,
        ),
        problem="needs a task manager",
        action=_action(),
        risk_tag=RiskTag(
            blast_radius="bot", reversibility="manual", touches=["app_install"]
        ),
        claim=None,
    )
    for status in ("pending", "approved_human", "applied"):
        transition(proposal, status, actor="test", reason="test")
    arbiter_store.write_proposal(proposal, tmp_path)

    counts = forge_sweep.sweep(
        tmp_path,
        job_loader=lambda job_id, shared: types.SimpleNamespace(status=job_status),
        log_fn=lambda _msg: None,
        stale_sweeper=lambda _shared: [],
    )
    assert counts["succeeded" if expected == "succeeded" else "failed"] == 1

    located = arbiter_store.find_proposal(tmp_path, proposal.id)
    assert located is not None
    assert located[0].status == expected


def test_forge_sweep_leaves_in_flight_installapp_alone(tmp_path):
    from arbiter import store as arbiter_store
    from arbiter.state_machine import transition
    from schema.proposal import Proposal, RiskTag, new_proposal_id
    from schema.provenance import Provenance

    proposal = Proposal(
        id=new_proposal_id(),
        bot_id=BOT,
        generator_id="fit_reviewer",
        dimension="capabilities",
        trigger_observations=["fit_review:y"],
        provenance=Provenance(
            technique="fit_reviewer.v1",
            signals={"_apply_details": {"job_id": "j-sweep-2"}},
            confidence=1.0,
        ),
        problem="needs a task manager",
        action=_action(),
        risk_tag=RiskTag(
            blast_radius="bot", reversibility="manual", touches=["app_install"]
        ),
        claim=None,
    )
    for status in ("pending", "approved_human", "applied"):
        transition(proposal, status, actor="test", reason="test")
    arbiter_store.write_proposal(proposal, tmp_path)

    counts = forge_sweep.sweep(
        tmp_path,
        job_loader=lambda job_id, shared: types.SimpleNamespace(status="running"),
        log_fn=lambda _msg: None,
        stale_sweeper=lambda _shared: [],
    )
    assert counts["in_flight"] == 1
    located = arbiter_store.find_proposal(tmp_path, proposal.id)
    assert located is not None and located[0].status == "applied"


def _install_proposal(status: str = "approved_human"):
    from arbiter.state_machine import transition
    from schema.proposal import Proposal, RiskTag, new_proposal_id
    from schema.provenance import Provenance

    proposal = Proposal(
        id=new_proposal_id(),
        bot_id=BOT,
        generator_id="fit_reviewer",
        dimension="capabilities",
        trigger_observations=["fit_review:z"],
        provenance=Provenance(technique="fit_reviewer.v1", signals={}, confidence=1.0),
        problem="needs a task manager",
        action=_action(),
        risk_tag=RiskTag(
            blast_radius="bot", reversibility="manual", touches=["app_install"]
        ),
        claim=None,
    )
    for step in ("pending", "approved_human"):
        transition(proposal, step, actor="test", reason="test")
    if status != "approved_human":
        transition(proposal, status, actor="test", reason="test")
    return proposal


def test_applied_installapp_does_not_auto_succeed(seam):
    """The end-to-end version of the external-completion contract.

    These proposals carry no Claim, so apply.py's claim-less branch would
    close them as ``succeeded`` the moment the job was queued — reporting an
    install that forge has not begun. It must stop at ``applied`` and wait
    for the sweep.
    """
    from arbiter import apply as arbiter_apply

    proposal = _install_proposal()
    outcome = arbiter_apply.apply(proposal, shared_dir=seam.shared)
    assert outcome.ok, outcome.message
    assert proposal.status == "applied"
    assert len(seam.rec.created) == 1


def test_apply_deferred_when_target_bot_breaker_tripped(seam):
    """An install writes to the bot, so a tripped breaker must hold it off.

    Membership in _EXTERNAL_COMPLETION_KINDS used to imply breaker exemption
    (both were read through is_deferred_completion_kind); this asserts the
    gate itself, not just the set's contents.
    """
    from arbiter import apply as arbiter_apply
    from breakers import store as _bstore

    _bstore.trip(
        shared_dir=seam.shared, scope=BOT, breaker_type="full",
        duration=None, initiated_by="test", reason="suppression test",
    )
    proposal = _install_proposal()
    outcome = arbiter_apply.apply(proposal, shared_dir=seam.shared)

    assert outcome.ok is False
    assert outcome.deferred is True
    assert proposal.status == "approved_human"  # untouched, retried next sweep
    assert seam.rec.created == []               # no forge job was queued


def test_buildapp_stays_breaker_exempt(seam):
    """The decoupling must not change any kind that shipped before it."""
    from arbiter import apply as arbiter_apply
    from arbiter.appliers import build_app
    from arbiter.state_machine import transition
    from breakers import store as _bstore
    from schema.proposal import BuildApp, Proposal, RiskTag, new_proposal_id
    from schema.provenance import Provenance

    build_app.set_shared_dir(seam.shared)
    build_app.set_kickoff_runner(lambda *a, **kw: None)
    try:
        _bstore.trip(
            shared_dir=seam.shared, scope=BOT, breaker_type="full",
            duration=None, initiated_by="test", reason="suppression test",
        )
        proposal = Proposal(
            id=new_proposal_id(),
            bot_id=BOT,
            generator_id="app_builder",
            dimension="capabilities",
            trigger_observations=["build:1"],
            provenance=Provenance(technique="t", signals={}, confidence=1.0),
            problem="needs an app",
            action=BuildApp(
                bot_id=BOT, app_id="new-app", app_name="New App",
                manifest={"name": "New App"},
            ),
            risk_tag=RiskTag(
                blast_radius="bot", reversibility="manual", touches=["app_install"]
            ),
            claim=None,
        )
        for step in ("pending", "approved_human"):
            transition(proposal, step, actor="test", reason="test")

        outcome = arbiter_apply.apply(proposal, shared_dir=seam.shared)
        assert outcome.deferred is False
    finally:
        build_app.set_kickoff_runner(None)
        build_app.set_shared_dir(Path("/Users/Shared/evolve"))


def test_apply_then_sweep_closes_the_proposal_end_to_end(seam):
    """The two halves, joined: the job id has to survive the handoff.

    apply() returns it in ``ApplyResult.details``; ``arbiter.apply`` copies
    those details into ``provenance.signals["_apply_details"]``; the sweep
    reads the job id from there. Every other test in this file supplies
    ``_apply_details`` by hand, which would keep passing if that copy were
    dropped.
    """
    from arbiter import apply as arbiter_apply
    from arbiter import store as arbiter_store

    proposal = _install_proposal()
    assert arbiter_apply.apply(proposal, shared_dir=seam.shared).ok
    assert proposal.status == "applied"
    assert (
        proposal.provenance.signals["_apply_details"]["job_id"] == seam.job.job_id
    )
    arbiter_store.write_proposal(proposal, seam.shared)

    seen = []

    def _loader(job_id, shared):
        seen.append(job_id)
        return types.SimpleNamespace(status="complete")

    counts = forge_sweep.sweep(
        seam.shared,
        job_loader=_loader,
        log_fn=lambda _msg: None,
        stale_sweeper=lambda _shared: [],
    )
    assert seen == [seam.job.job_id]   # the sweep looked up the job apply made
    assert counts["succeeded"] == 1
    located = arbiter_store.find_proposal(seam.shared, proposal.id)
    assert located is not None and located[0].status == "succeeded"


def test_production_runner_carries_the_install_actor_and_label(monkeypatch):
    """With no test override, the dispatch must reach the shared forge tail.

    The runner is a partial rather than a named function, so nothing else in
    the suite exercises it — without this, removing the auto-approve actor
    (which is what lets an Act-approved install past forge's operator gate)
    would break nothing visible.
    """
    from arbiter.appliers import install_app

    calls = []
    monkeypatch.setattr(install_app, "_kickoff_runner", None)
    monkeypatch.setattr(
        install_app,
        "run_forge_job_kickoff",
        lambda shared, job_id, bot_id, **kw: calls.append((shared, job_id, bot_id, kw)),
    )
    install_app._resolve_runner()(Path("/tmp/shared"), "j-9", BOT)

    assert calls == [
        (Path("/tmp/shared"), "j-9", BOT,
         {"auto_approve_actor": "api:rsi-installapp", "log_label": "install_app"})
    ]
