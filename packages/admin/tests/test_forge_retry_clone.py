"""Tests for the clone-on-retry path + orphan-step janitor.

Background (2026-06-05): forge job j-9093a7a3 ended up in incoherent
state after multi-cycle in-place retries — status=failed at step 2,
yet step 4 still showed status=running with a started_at and no
finished_at. The orphan stayed there forever because no code path
ever reconciled it. The UI showed a spinning step on a job the
operator knew was done; clicking Retry again produced no observable
effect.

This PR replaces the in-place ``reset_to_queued`` retry with
``clone_for_retry`` — every retry creates a new job_id linked to the
prior via ``prior_job_id`` / ``superseded_by_job_id``. The orphan
janitor (``reconcile_orphan_steps`` / ``sweep_orphan_steps``) is the
safety net for any orphan state already on disk or written by an
older code path.

Coverage:
- clone_for_retry: identity inherit, fresh steps, run_id increment,
  audit chain, terminal-status guard, status/cost clean slate
- reconcile_orphan_steps: terminal-status job with running step →
  cancelled, non-terminal no-op, idempotency
- sweep_orphan_steps: batch over active dir
- Multi-cycle: dispatch → fail → retry → fail → retry → fail
  produces three distinct jobs, all linked, no orphans accumulate
- forge_sweep integration: orphan reconcile runs inside the sweep
  loop and is reflected in the counts dict
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evolve_admin.applications.forge_jobs import (  # noqa: E402
    ForgeJob,
    ForgeStep,
    TERMINAL_JOB_STATES,
    _install_steps,
    _improvement_steps,
    clone_for_retry,
    load_job,
    mark_step_failed,
    new_job_id,
    now_iso,
    reconcile_orphan_steps,
    run_id_for,
    save_job,
    sweep_orphan_steps,
)


@pytest.fixture(autouse=True)
def _route_manifests_to_tmp(tmp_path, monkeypatch):
    """Mirrors test_forge_retry — manifests resolve to tmp."""
    def _appdir(shared_dir, bot_id):
        out = tmp_path / "workspaces" / bot_id / "manifests"
        out.mkdir(parents=True, exist_ok=True)
        return out
    from evolve_admin.applications import manifest as _mf
    monkeypatch.setattr(_mf, "applications_dir", _appdir)


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _make_terminal_job(
    tmp_path: Path,
    *,
    status: str = "failed",
    job_type: str = "install",
    populate_context: bool = True,
    orphan_step_num: int | None = None,
) -> ForgeJob:
    """Persist a terminal-status job. If ``orphan_step_num`` is given,
    that step is left as ``running`` with a started_at and no
    finished_at — the exact shape of the j-9093a7a3 bug."""
    steps = _install_steps() if job_type == "install" else _improvement_steps()
    if orphan_step_num is not None:
        for s in steps:
            if s.num == orphan_step_num:
                s.status = "running"
                s.started_at = now_iso()
                s.finished_at = None
                s.detail = "in flight when prior dispatch died"
            elif s.num < orphan_step_num:
                s.status = "done"
                s.started_at = now_iso()
                s.finished_at = now_iso()
                s.detail = "ran"
        # And mark step 2 (the actual cause of the terminal failure) as failed
        for s in steps:
            if s.num == 2 and s.status != "running":
                s.status = "failed"
                s.started_at = now_iso()
                s.finished_at = now_iso()
                s.detail = "boom"
    job = ForgeJob(
        job_id=new_job_id(),
        run_id=run_id_for(1),
        job_type=job_type,
        pkg_id="p-12345678",
        app_id="test-app",
        bot_id="bot_test",
        pkg_version_before=None,
        gallery_version="v1.0.0" if job_type == "install" else None,
        steps=steps,
        current_step=2 if orphan_step_num else 0,
        created_at=now_iso(),
        last_updated=now_iso(),
        status=status,
        critique_rounds_done=2,
        issues_found=4,
        issues_resolved=3,
        issues_deferred=1,
        test_exit_code=1,
        test_output_summary="2 of 5 tests failed",
        operator_confirmed=True,
        projected_cost_mid_usd=0.42,
        projected_cost_high_usd=1.08,
        actual_cost_usd=0.55,
        triggered_by_detector=None,
        context_snapshot={"prior_run_garbage": True} if populate_context else {},
    )
    save_job(job, tmp_path)
    return job


# ─────────────────────────────────────────────────────────────────────
# clone_for_retry — identity inherit + fresh state
# ─────────────────────────────────────────────────────────────────────


class TestCloneForRetry:

    def test_clone_has_new_job_id(self, tmp_path):
        prior = _make_terminal_job(tmp_path)
        clone = clone_for_retry(prior, tmp_path)
        assert clone.job_id != prior.job_id
        assert clone.job_id.startswith("j-")

    def test_clone_starts_queued_with_current_step_zero(self, tmp_path):
        prior = _make_terminal_job(tmp_path)
        clone = clone_for_retry(prior, tmp_path)
        assert clone.status == "queued"
        assert clone.current_step == 0

    def test_clone_inherits_identity_fields(self, tmp_path):
        prior = _make_terminal_job(tmp_path)
        clone = clone_for_retry(prior, tmp_path)
        for field in ("job_type", "pkg_id", "app_id", "bot_id",
                      "gallery_version", "pkg_version_before",
                      "triggered_by_detector", "operator_confirmed",
                      "projected_cost_mid_usd", "projected_cost_high_usd"):
            assert getattr(clone, field) == getattr(prior, field), (
                f"clone.{field} should inherit from prior"
            )

    def test_clone_drops_run_state_and_costs(self, tmp_path):
        """A clone is a fresh attempt — issue counts, test result,
        rejection metadata, actual cost, context_snapshot all reset."""
        prior = _make_terminal_job(tmp_path)
        clone = clone_for_retry(prior, tmp_path)
        assert clone.critique_rounds_done == 0
        assert clone.issues_found == 0
        assert clone.issues_resolved == 0
        assert clone.issues_deferred == 0
        assert clone.test_exit_code is None
        assert clone.test_output_summary == ""
        assert clone.reject_reason == ""
        assert clone.rejected_by == ""
        assert clone.rejected_at == ""
        assert clone.actual_cost_usd is None
        assert clone.context_snapshot == {}

    def test_clone_run_id_increments(self, tmp_path):
        """Clone gets the next run_number in the (pkg_id, bot_id)
        sequence. Multiple clones produce a strictly-increasing run_id."""
        prior = _make_terminal_job(tmp_path)
        clone1 = clone_for_retry(prior, tmp_path)
        clone2 = clone_for_retry(clone1._mark_failed(tmp_path), tmp_path)
        assert clone1.run_id != prior.run_id
        assert clone2.run_id != clone1.run_id
        # Strictly increasing
        n_prior = int(prior.run_id.split("-")[1])
        n_clone1 = int(clone1.run_id.split("-")[1])
        n_clone2 = int(clone2.run_id.split("-")[1])
        assert n_clone1 > n_prior
        assert n_clone2 > n_clone1

    def test_clone_steps_are_fresh_seed(self, tmp_path):
        """The clone's steps must match the factory seed exactly — no
        started_at / finished_at / detail bleed from the prior. Status
        matches whatever the factory returns (install step 9 seeds as
        ``waiting`` for the auto-ship row; everything else pending)."""
        prior = _make_terminal_job(tmp_path, orphan_step_num=4)
        clone = clone_for_retry(prior, tmp_path)
        seed = _install_steps()  # canonical seed for an install job
        seed_status_by_num = {s.num: s.status for s in seed}
        for step in clone.steps:
            assert step.started_at is None, (
                f"step {step.num} should have no started_at on a fresh clone"
            )
            assert step.finished_at is None
            assert step.detail == ""
            assert step.status == seed_status_by_num[step.num], (
                f"step {step.num} should match factory seed status"
            )

    def test_clone_links_prior_via_prior_job_id(self, tmp_path):
        prior = _make_terminal_job(tmp_path)
        clone = clone_for_retry(prior, tmp_path)
        assert clone.prior_job_id == prior.job_id

    def test_prior_marked_superseded_by_clone(self, tmp_path):
        """Loading the prior after a clone shows superseded_by_job_id
        pointing at the clone. UI uses this to render the chain."""
        prior = _make_terminal_job(tmp_path)
        clone = clone_for_retry(prior, tmp_path)
        reloaded_prior = load_job(prior.job_id, tmp_path)
        assert reloaded_prior is not None
        assert reloaded_prior.superseded_by_job_id == clone.job_id
        # Status stays terminal — the prior IS frozen.
        assert reloaded_prior.status in TERMINAL_JOB_STATES

    def test_clone_reconciles_prior_orphan_steps(self, tmp_path):
        """The whole point: the prior's orphan running step gets
        closed out as part of the clone operation. Without this, the
        UI would still show the orphan spinning forever even after
        the operator clicked retry."""
        prior = _make_terminal_job(tmp_path, orphan_step_num=4)
        # Sanity: the orphan is present before cloning
        orphan = next(s for s in prior.steps if s.num == 4)
        assert orphan.status == "running"
        assert orphan.started_at is not None
        assert orphan.finished_at is None

        clone = clone_for_retry(prior, tmp_path)
        reloaded_prior = load_job(prior.job_id, tmp_path)
        reconciled = next(s for s in reloaded_prior.steps if s.num == 4)
        assert reconciled.status == "cancelled"
        assert reconciled.finished_at is not None
        # The detail should mention the clone id so an operator who
        # clicks on the step sees where the work moved to.
        assert clone.job_id in reconciled.detail

    def test_clone_refuses_non_terminal_prior(self, tmp_path):
        """Cannot clone a job that's still in flight — would race
        with the dispatch thread."""
        prior = _make_terminal_job(tmp_path, status="running")
        with pytest.raises(ValueError, match="non-terminal"):
            clone_for_retry(prior, tmp_path)

    @pytest.mark.parametrize("status", ["failed", "cancelled", "rejected", "complete"])
    def test_clone_accepts_all_terminal_statuses(self, tmp_path, status):
        prior = _make_terminal_job(tmp_path, status=status)
        clone = clone_for_retry(prior, tmp_path)
        assert clone.status == "queued"

    def test_clone_of_improvement_uses_improvement_steps(self, tmp_path):
        """Step labels differ by job_type — improvement has
        ``AWAIT: operator approval``, install has ``Approval (auto-ship
        for installs)``. The clone must pick the right factory."""
        prior = _make_terminal_job(tmp_path, job_type="improvement")
        clone = clone_for_retry(prior, tmp_path)
        labels = [s.label for s in clone.steps]
        assert any("AWAIT: operator approval" in label for label in labels)
        assert not any("auto-ship for installs" in label for label in labels)


# Tiny helper attached to ForgeJob for the run_id-increment test ─────
# Marks the job failed and persists, so the next clone_for_retry call
# sees a terminal-status job to clone.
def _mark_failed(self: ForgeJob, shared_dir: Path) -> ForgeJob:
    self.status = "failed"
    save_job(self, shared_dir)
    return self
ForgeJob._mark_failed = _mark_failed  # type: ignore[attr-defined]


# ─────────────────────────────────────────────────────────────────────
# reconcile_orphan_steps — janitor
# ─────────────────────────────────────────────────────────────────────


class TestReconcileOrphanSteps:

    def test_reconciles_running_step_on_failed_job(self, tmp_path):
        """The j-9093a7a3 shape: failed job + step still marked running."""
        job = _make_terminal_job(tmp_path, status="failed", orphan_step_num=4)
        fixed = reconcile_orphan_steps(job, tmp_path)
        assert 4 in fixed
        reloaded = load_job(job.job_id, tmp_path)
        step = next(s for s in reloaded.steps if s.num == 4)
        assert step.status == "cancelled"
        assert step.finished_at is not None

    def test_reconciles_started_but_not_finished_step(self, tmp_path):
        """A step that has a started_at but no finished_at and isn't in
        a final status — same orphan shape regardless of the status string."""
        job = _make_terminal_job(tmp_path, status="failed")
        target = next(s for s in job.steps if s.num == 5)
        target.status = "pending"  # not "running" — but still mid-flight
        target.started_at = now_iso()
        target.finished_at = None
        save_job(job, tmp_path)

        fixed = reconcile_orphan_steps(job, tmp_path)
        assert 5 in fixed
        reloaded = load_job(job.job_id, tmp_path)
        step = next(s for s in reloaded.steps if s.num == 5)
        assert step.status == "cancelled"
        assert step.finished_at is not None

    def test_no_op_for_non_terminal_job(self, tmp_path):
        """A running job is allowed to have a running step. The
        janitor must not interfere with live work."""
        job = _make_terminal_job(tmp_path, status="failed", orphan_step_num=4)
        job.status = "running"  # flip back to live
        save_job(job, tmp_path)
        fixed = reconcile_orphan_steps(job, tmp_path)
        assert fixed == []
        reloaded = load_job(job.job_id, tmp_path)
        step = next(s for s in reloaded.steps if s.num == 4)
        assert step.status == "running"  # untouched

    def test_idempotent(self, tmp_path):
        """Running twice fixes orphans once. The second call is a no-op."""
        job = _make_terminal_job(tmp_path, status="failed", orphan_step_num=4)
        fixed1 = reconcile_orphan_steps(job, tmp_path)
        assert 4 in fixed1
        reloaded = load_job(job.job_id, tmp_path)
        fixed2 = reconcile_orphan_steps(reloaded, tmp_path)
        assert fixed2 == []

    def test_preserves_prior_detail_when_reconciling(self, tmp_path):
        """If the orphan step carried a useful detail (e.g. '3 issues
        found'), the janitor prefixes its reason but keeps the prior."""
        job = _make_terminal_job(tmp_path, status="failed", orphan_step_num=4)
        fixed = reconcile_orphan_steps(
            job, tmp_path, reason="abandoned during cleanup",
        )
        assert 4 in fixed
        reloaded = load_job(job.job_id, tmp_path)
        step = next(s for s in reloaded.steps if s.num == 4)
        assert "abandoned during cleanup" in step.detail
        # The prior detail is preserved
        assert "in flight when prior dispatch died" in step.detail

    def test_skips_complete_jobs_with_no_orphans(self, tmp_path):
        """Complete jobs never have orphans (every step closed before
        complete_job). Janitor returns empty list, never touches state."""
        steps = _install_steps()
        for s in steps:
            s.status = "done"
            s.started_at = now_iso()
            s.finished_at = now_iso()
        job = ForgeJob(
            job_id=new_job_id(), run_id=run_id_for(1), job_type="install",
            pkg_id="p-aaaa1111", app_id="ok-app", bot_id="bot_x",
            pkg_version_before=None, gallery_version="v1",
            steps=steps, status="complete",
            created_at=now_iso(), last_updated=now_iso(),
        )
        save_job(job, tmp_path)
        assert reconcile_orphan_steps(job, tmp_path) == []


# ─────────────────────────────────────────────────────────────────────
# sweep_orphan_steps — batch janitor
# ─────────────────────────────────────────────────────────────────────


class TestSweepOrphanSteps:

    def test_sweep_walks_active_dir_and_reconciles(self, tmp_path):
        """Two jobs with orphan steps + one clean → sweep reconciles
        the two and reports counts."""
        j1 = _make_terminal_job(tmp_path, status="failed", orphan_step_num=4)
        j2 = _make_terminal_job(tmp_path, status="rejected", orphan_step_num=6)
        j_clean = _make_terminal_job(tmp_path, status="cancelled")  # no orphan

        result = sweep_orphan_steps(tmp_path)
        assert result["jobs_reconciled"] == 2
        assert result["steps_reconciled"] == 2

        # Both got reconciled
        for jid, step_num in ((j1.job_id, 4), (j2.job_id, 6)):
            reloaded = load_job(jid, tmp_path)
            step = next(s for s in reloaded.steps if s.num == step_num)
            assert step.status == "cancelled"

        # Clean job is unchanged
        reloaded = load_job(j_clean.job_id, tmp_path)
        assert all(s.status != "cancelled" for s in reloaded.steps)

    def test_sweep_idempotent(self, tmp_path):
        _make_terminal_job(tmp_path, status="failed", orphan_step_num=4)
        first = sweep_orphan_steps(tmp_path)
        assert first["steps_reconciled"] == 1
        second = sweep_orphan_steps(tmp_path)
        assert second["steps_reconciled"] == 0
        assert second["jobs_reconciled"] == 0

    def test_sweep_skips_non_terminal_jobs(self, tmp_path):
        """A running job with a running step is in-flight, not orphaned."""
        running = _make_terminal_job(tmp_path, status="failed", orphan_step_num=4)
        running.status = "running"
        save_job(running, tmp_path)

        result = sweep_orphan_steps(tmp_path)
        assert result["jobs_reconciled"] == 0
        # The step stays in its original state
        reloaded = load_job(running.job_id, tmp_path)
        step = next(s for s in reloaded.steps if s.num == 4)
        assert step.status == "running"


# ─────────────────────────────────────────────────────────────────────
# Multi-cycle retry: dispatch → fail → retry → fail → retry → fail
# Each cycle produces a distinct job_id; the chain is navigable both
# directions; no orphan state accumulates.
# ─────────────────────────────────────────────────────────────────────


class TestMultiCycleRetry:

    def _fail_in_place(self, job: ForgeJob, tmp_path: Path, step_num: int) -> None:
        """Simulate the bot dispatch failing at ``step_num`` — leaves
        the job in status=failed with that step marked failed."""
        mark_step_failed(job, step_num, "simulated build failure", tmp_path)

    def test_three_cycle_retry_no_orphans(self, tmp_path):
        """Dispatch → fail → retry → fail → retry → fail. Each cycle
        gets its own job_id; all three are linked; no job has orphan
        running steps at the end."""
        # Cycle 1: original
        original = _make_terminal_job(
            tmp_path, status="queued", populate_context=False,
        )
        original.status = "queued"
        save_job(original, tmp_path)
        self._fail_in_place(original, tmp_path, step_num=2)

        # Cycle 2: first retry
        retry1 = clone_for_retry(original, tmp_path)
        self._fail_in_place(retry1, tmp_path, step_num=4)

        # Cycle 3: second retry
        retry2 = clone_for_retry(retry1, tmp_path)
        self._fail_in_place(retry2, tmp_path, step_num=2)

        # All three distinct
        assert len({original.job_id, retry1.job_id, retry2.job_id}) == 3

        # Audit chain links both ways
        prior_chain = []
        cur = load_job(retry2.job_id, tmp_path)
        while cur is not None:
            prior_chain.append(cur.job_id)
            cur = load_job(cur.prior_job_id, tmp_path) if cur.prior_job_id else None
        assert prior_chain == [retry2.job_id, retry1.job_id, original.job_id]

        # Forward chain via superseded_by_job_id
        forward_chain = []
        cur = load_job(original.job_id, tmp_path)
        while cur is not None:
            forward_chain.append(cur.job_id)
            cur = (
                load_job(cur.superseded_by_job_id, tmp_path)
                if cur.superseded_by_job_id else None
            )
        assert forward_chain == [original.job_id, retry1.job_id, retry2.job_id]

        # Sweep should find no orphans across the whole chain — every
        # mid-flight step was reconciled at clone time, and only the
        # explicitly-failed step is in a non-pending state on each prior.
        result = sweep_orphan_steps(tmp_path)
        assert result == {"jobs_reconciled": 0, "steps_reconciled": 0}

    def test_in_place_orphan_then_clone_recovers(self, tmp_path):
        """The j-9093a7a3 reproduction. Prior left in incoherent state
        (failed + orphan running step). Cloning recovers cleanly."""
        # Simulate the bug: failed job with an orphan running step.
        prior = _make_terminal_job(
            tmp_path, status="failed", orphan_step_num=4,
        )
        # Confirm the orphan exists
        orphan = next(s for s in prior.steps if s.num == 4)
        assert orphan.status == "running"
        assert orphan.finished_at is None

        # Click retry → clone path runs, including the orphan reconcile
        clone = clone_for_retry(prior, tmp_path)

        # Clone is clean
        assert clone.status == "queued"
        assert all(
            s.status in ("pending", "waiting") for s in clone.steps
        )
        # Prior is reconciled
        reloaded_prior = load_job(prior.job_id, tmp_path)
        reconciled_orphan = next(
            s for s in reloaded_prior.steps if s.num == 4
        )
        assert reconciled_orphan.status == "cancelled"
        assert reconciled_orphan.finished_at is not None


# ─────────────────────────────────────────────────────────────────────
# forge_sweep integration — orphan janitor runs inside the sweep cycle
# ─────────────────────────────────────────────────────────────────────


class TestForgeSweepIntegration:

    def test_sweep_counts_include_orphan_reconcile(self, tmp_path):
        """The forge_sweep.sweep() entry point runs sweep_orphan_steps
        and surfaces the count in its return dict so the daily log line
        captures it."""
        from arbiter.forge_sweep import sweep

        _make_terminal_job(tmp_path, status="failed", orphan_step_num=4)
        _make_terminal_job(tmp_path, status="rejected", orphan_step_num=6)

        # No-op loaders so the proposal pass doesn't try to load
        # real arbiter state from disk.
        def _no_proposals(_id, _shared_dir):
            return None

        def _no_stale(_shared_dir):
            return []

        counts = sweep(
            tmp_path,
            job_loader=_no_proposals,
            stale_sweeper=_no_stale,
            log_fn=lambda _msg: None,
        )
        assert counts["orphan_steps_reconciled"] == 2
        assert counts["orphan_jobs_reconciled"] == 2
