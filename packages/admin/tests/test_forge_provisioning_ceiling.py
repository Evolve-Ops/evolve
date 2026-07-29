"""Forge honors the provisioning budget gate (decision B; META:user-value).

The gate's own math is proven in
packages/analyzer/tests/test_provisioning_budget.py. THIS file is the
call-site proof — that ``forge_engine.run_forge_job`` actually refuses a
provisioning build when the budget gate says stop, and ends the bot in a
sane, observable state. It pins the two auditor-grade failure modes the
backstop exists to prevent:

  (i)  "the ceiling never enforces (bleed continues)" — a refused install
       must NOT dispatch the build (``_run_bot_dispatch`` is never reached),
       so no LLM tokens are billed past the cap.
  (ii) "the ceiling enforces but bricks the bot half-provisioned with no
       signal" — the refused job ends ``failed`` with a budget reason, an
       observable Signal is emitted, and (run sequentially, the way the
       starter-pack installer runs) apps built before the cap stay complete
       while the rest stop cleanly: complete-current-then-stop.

Background: ``ledger`` spent $30.26 in two days building + auditing its own
starter apps; the $10 daily cap tripped twice but couldn't halt the spend
because forge runs outside the L1 breaker scope.
Finding: docs/finding-new-bot-activation-cost-2026-06-12.md.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for _p in (str(_ADMIN), str(_ANALYZER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import provisioning_budget as pb  # noqa: E402


def _make_install_job(job_id: str = "j-test1234", app_id: str = "journal"):
    from evolve_admin.applications.forge_jobs import ForgeJob, _install_steps
    return ForgeJob(
        job_id=job_id,
        run_id="r-00000001",
        job_type="install",
        pkg_id="p-test1234",
        app_id=app_id,
        bot_id="ledger",
        pkg_version_before=None,
        gallery_version=None,
        steps=_install_steps(),
        status="queued",
    )


def _fake_gallery_pkg():
    return {
        "pkg_id": "p-test1234",
        "name": "journal",
        "display_name": "Journal",
        "objective": "test app",
        "build_spec": "n/a",
    }


def _refused_decision(reason="cumulative standup spend $13.00 reached $12.00 ceiling"):
    return pb.ProvisioningBudgetDecision(
        allowed=False,
        failed_check="creation_ceiling",
        reason=reason,
        ceiling_usd=12.0,
        spent_usd=13.0,
        remaining_usd=0.0,
        in_window=True,
        breaker_tripped=False,
        signal_payload={
            "signature": "provisioning_budget:provisioning_budget_exceeded:ledger/creation_ceiling",
            "producer": "provisioning_budget",
            "type": "provisioning_budget_exceeded",
            "severity": "warn",
            "scope": "bot",
            "bot_id": "ledger",
            "title": "ledger: provisioning paused",
            "body": "…",
            "details": {"failed_check": "creation_ceiling"},
        },
    )


# ── (i) refusal actually stops the build (no bleed) ──────────────────────────


def test_over_ceiling_install_refuses_before_any_build(tmp_path, monkeypatch):
    from evolve_admin.applications import forge_engine

    emitted: list[dict] = []
    monkeypatch.setattr(pb, "evaluate", lambda *a, **k: _refused_decision())
    monkeypatch.setattr(pb, "emit_signal", lambda sd, payload: emitted.append(payload) or True)

    job = _make_install_job()
    with patch.object(forge_engine, "load_job", return_value=job), \
         patch.object(forge_engine, "_run_bot_dispatch") as dispatch, \
         patch.object(forge_engine, "approve_forge_job") as approve, \
         patch.object(forge_engine, "_append_log"):
        forge_engine.run_forge_job(
            job_id=job.job_id, shared_dir=tmp_path, bot_id="ledger",
        )

    # (i) the build never ran — no LLM cost past the cap.
    dispatch.assert_not_called()
    approve.assert_not_called()
    # (ii) sane + observable terminal state.
    assert job.status == "failed"
    assert emitted and emitted[0]["type"] == "provisioning_budget_exceeded"
    # The failed step carries the budget reason (operator sees WHY on the
    # Forge Jobs page + in the post-wrap install-failed notification).
    failed = [s for s in job.steps if s.status == "failed"]
    assert failed and "ceiling" in (failed[0].detail or "")


def test_under_budget_install_proceeds(tmp_path, monkeypatch):
    """No regression: an in-budget install runs the normal pipeline."""
    from evolve_admin.applications import forge_engine

    monkeypatch.setattr(
        pb, "evaluate",
        lambda *a, **k: pb.ProvisioningBudgetDecision(allowed=True, ceiling_usd=12.0),
    )

    captured_actor: list[str] = []

    def fake_approve(job_id, shared_dir, *, approved_by, notes=""):
        captured_actor.append(approved_by)

    job = _make_install_job()
    with patch.object(forge_engine, "load_job", return_value=job), \
         patch.object(forge_engine, "_run_bot_dispatch") as dispatch, \
         patch.object(forge_engine, "_run_integration_check"), \
         patch.object(forge_engine, "_resolve_api_key", return_value="sk-test"), \
         patch.object(forge_engine, "_get_models",
                      return_value=("anthropic/claude-sonnet-4-6", "haiku")), \
         patch.object(forge_engine, "approve_forge_job", side_effect=fake_approve), \
         patch.object(forge_engine, "_append_log"), \
         patch.object(forge_engine, "load_manifest", return_value=None), \
         patch.object(forge_engine, "save_manifest"), \
         patch("evolve_admin.applications.gallery.load_gallery_package",
               return_value=_fake_gallery_pkg()), \
         patch.object(forge_engine, "assemble_context_package", return_value={}), \
         patch.object(forge_engine, "_get_critique_rounds", return_value=2):
        forge_engine.run_forge_job(
            job_id=job.job_id, shared_dir=tmp_path, bot_id="ledger",
        )

    dispatch.assert_called()           # the build ran
    assert captured_actor == ["forge_install_auto"]


# ── (ii) complete-current-then-stop across a sequential standup ──────────────


def test_sequential_standup_completes_then_stops(tmp_path, monkeypatch):
    """The starter-pack installer runs jobs sequentially in one worker. With a
    real-ish accumulating spend, builds proceed until the standup crosses the
    ceiling, then the remaining jobs stop cleanly — apps already built stay
    complete; nothing is half-written; each refusal is observable.
    """
    from evolve_admin.applications import forge_engine

    state = {"spent": 0.0}
    CEILING = 12.0
    PER_BUILD = 5.0

    # Real-ish gate: refuse once cumulative standup spend has reached the
    # ceiling (the same predicate the production gate applies, fed by a
    # simulated ledger the build mock advances).
    def fake_evaluate(bot_id, shared_dir, *, kind, job_id="", remaining_label="",
                      now=None, network_path=None):
        spent = state["spent"]
        allowed, check, reason = pb.decide(
            spent_usd=spent, ceiling_usd=CEILING,
            in_window=True, breaker_tripped=False,
        )
        if allowed:
            return pb.ProvisioningBudgetDecision(allowed=True, ceiling_usd=CEILING,
                                                 spent_usd=spent)
        return pb.ProvisioningBudgetDecision(
            allowed=False, failed_check=check, reason=reason,
            ceiling_usd=CEILING, spent_usd=spent,
            signal_payload=pb.build_capped_signal(
                bot_id=bot_id, kind=kind, job_id=job_id, failed_check=check,
                reason=reason, ceiling_usd=CEILING, spent_usd=spent,
                remaining_label=remaining_label,
            ),
        )

    emitted: list[dict] = []
    monkeypatch.setattr(pb, "evaluate", fake_evaluate)
    monkeypatch.setattr(pb, "emit_signal", lambda sd, p: emitted.append(p) or True)

    # Reaching the build dispatch == this app got provisioned; it also advances
    # the simulated standup ledger the gate reads.
    dispatched: list[str] = []

    def fake_dispatch(job, context, shared_dir, critique_rounds):
        dispatched.append(job.job_id)
        state["spent"] += PER_BUILD

    jobs = {f"j-{i}": _make_install_job(job_id=f"j-{i}", app_id=f"app{i}")
            for i in range(1, 5)}

    def fake_load_job(job_id, shared_dir):
        return jobs[job_id]

    with patch.object(forge_engine, "load_job", side_effect=fake_load_job), \
         patch.object(forge_engine, "_run_bot_dispatch", side_effect=fake_dispatch), \
         patch.object(forge_engine, "_run_integration_check"), \
         patch.object(forge_engine, "_resolve_api_key", return_value="sk-test"), \
         patch.object(forge_engine, "_get_models",
                      return_value=("anthropic/claude-sonnet-4-6", "haiku")), \
         patch.object(forge_engine, "approve_forge_job"), \
         patch.object(forge_engine, "_append_log"), \
         patch.object(forge_engine, "load_manifest", return_value=None), \
         patch.object(forge_engine, "save_manifest"), \
         patch("evolve_admin.applications.gallery.load_gallery_package",
               return_value=_fake_gallery_pkg()), \
         patch.object(forge_engine, "assemble_context_package", return_value={}), \
         patch.object(forge_engine, "_get_critique_rounds", return_value=2):
        # Sequential worker, exactly like queue_pack_installs._worker.
        for jid in ("j-1", "j-2", "j-3", "j-4"):
            forge_engine.run_forge_job(job_id=jid, shared_dir=tmp_path, bot_id="ledger")

    # complete-current-then-stop: builds proceed until the standup crosses $12
    # (3 × $5 = $15), then the remaining job is refused before its build.
    assert dispatched == ["j-1", "j-2", "j-3"], (
        f"apps before the cap must build; got dispatched={dispatched}"
    )
    assert "j-4" not in dispatched              # refused before any build
    assert jobs["j-4"].status == "failed"       # sane terminal state, not half-built
    j4_failed = [s for s in jobs["j-4"].steps if s.status == "failed"]
    assert j4_failed and "ceiling" in (j4_failed[0].detail or "")
    # The cap is observable, not silent.
    assert emitted, "a provisioning-capped Signal must be emitted on refusal"
    assert emitted[-1]["type"] == "provisioning_budget_exceeded"
    # Overshoot is bounded to a single build past the ceiling (the in-flight
    # app completes; nothing runs unboundedly the way ledger's 8 builds did).
    assert state["spent"] == pytest.approx(15.0)
