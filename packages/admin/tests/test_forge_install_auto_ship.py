"""tests/test_forge_install_auto_ship.py — install jobs auto-ship behavior.

Background:
Pod_admin's atlas-daily-digest forge run made it to the approval gate on
2026-05-28 — 2 critique rounds, 17 issues found, 13 resolved, tests
passed. But the approval modal showed `(not available)` for both
"Generated implementation" and "Interface contract", so there was
literally nothing for the operator to review. Net: a gate with no
signal.

Decision: install jobs auto-ship. Rationale:
  - No prior version to diff (the "approve a delta" mental model
    doesn't apply to first-time installs).
  - The operator's "do I want this app?" decision happened earlier,
    at gallery install time.
  - Tests already passed (otherwise we'd never reach Step 8).
  - Critique converged.
  - Operator can audit via the manifest button after the fact, edit
    freely, or reinstall.

Improvement jobs KEEP the approval gate — they have a real before/after
and a meaningful approve-or-revert decision.

Coverage:
  - Install jobs auto-set `auto_approve_actor` to `forge_install_auto`
    when the caller didn't pass one
  - Improvement jobs leave `auto_approve_actor` as None → existing
    operator-approval gate applies
  - When the caller explicitly passes `auto_approve_actor`, that wins
    over the install-job auto-trigger (messaging-driven flow preserved)
  - Notes prose distinguishes the two auto-approve paths so history
    reads truthfully
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for _p in (str(_ADMIN), str(_ANALYZER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _make_fake_job(job_type: str = "install"):
    """Build a real ForgeJob for the trigger checks.

    Run_forge_job calls mark_step_running/done internally which
    serializes the job via asdict — needs a real dataclass instance,
    not a MagicMock. shared_dir is a tmp path so save_job's atomic
    writes land somewhere safe.
    """
    from evolve_admin.applications.forge_jobs import ForgeJob, _install_steps
    return ForgeJob(
        job_id="j-test1234",
        run_id="r-00000001",
        job_type=job_type,
        pkg_id="p-test1234",
        app_id="test-app",
        bot_id="test-bot",
        pkg_version_before=None,
        gallery_version=None,
        steps=_install_steps(),
        status="queued",
    )


def _fake_gallery_pkg():
    """Minimal gallery package dict for tests that exercise the Step 1 seed path.

    ApplicationManifest.from_dict requires id (set by Step 1 from job.app_id),
    name (must come from the pkg), and bot_id (set by Step 1 from job.bot_id).
    """
    return {
        "pkg_id": "p-test1234",
        "name": "test-app",
        "display_name": "Test App",
        "objective": "test app for unit tests",
        "build_spec": "n/a",
    }


# ── Auto-set on install ──────────────────────────────────────────────────────


def test_install_job_with_no_caller_actor_auto_sets_install_actor(tmp_path):
    """A queued install with no explicit auto_approve_actor must have one
    set internally so the existing skip-step-8 path runs.

    Strategy: patch _run_bot_dispatch / _run_integration_check /
    _resolve_api_key / _get_models to no-ops, intercept approve_forge_job
    to record the actor we got called with, then verify it matches the
    new sentinel.
    """
    from evolve_admin.applications import forge_engine

    captured_approve_actor: list[str] = []
    captured_approve_notes: list[str] = []

    def fake_approve(job_id, shared_dir, *, approved_by, notes=""):
        captured_approve_actor.append(approved_by)
        captured_approve_notes.append(notes)

    job = _make_fake_job(job_type="install")
    with patch.object(forge_engine, "load_job", return_value=job), \
         patch.object(forge_engine, "_run_bot_dispatch"), \
         patch.object(forge_engine, "_run_integration_check"), \
         patch.object(forge_engine, "_resolve_api_key", return_value="sk-test"), \
         patch.object(forge_engine, "_get_models", return_value=("anthropic/claude-sonnet-4-6", "haiku")), \
         patch.object(forge_engine, "approve_forge_job", side_effect=fake_approve), \
         patch.object(forge_engine, "_append_log"), \
         patch.object(forge_engine, "load_manifest", return_value=None), \
         patch.object(forge_engine, "save_manifest"), \
         patch("evolve_admin.applications.gallery.load_gallery_package",
               return_value=_fake_gallery_pkg()), \
         patch.object(forge_engine, "assemble_context_package", return_value={}), \
         patch.object(forge_engine, "_get_critique_rounds", return_value=2):
        forge_engine.run_forge_job(
            job_id="j-test1234",
            shared_dir=tmp_path,
            bot_id="test-bot",
            auto_approve_actor=None,
        )

    assert captured_approve_actor == ["forge_install_auto"], (
        f"install job should auto-set actor='forge_install_auto'; "
        f"got {captured_approve_actor!r}"
    )
    # Notes prose distinguishes this from messaging-driven auto-approves
    assert any("install" in n for n in captured_approve_notes)
    assert any("operator can audit" in n for n in captured_approve_notes)


def test_auto_approve_transitions_status_to_awaiting_before_approve_call(tmp_path):
    """REGRESSION: approve_forge_job() requires job.status == 'awaiting_approval'.
    The auto-approve block previously called approve_forge_job() directly
    with the job still in 'running' state — atlas's j-a9bc8b38 hit this
    2026-05-29 with: 'Auto-approve failed: job is in state running, cannot
    approve'. The fix calls mark_awaiting_approval() between mark_step_running(8)
    and approve_forge_job(). Order matters: status must transition before
    approve is invoked."""
    from evolve_admin.applications import forge_engine

    transitions: list[str] = []

    def record_awaiting(job, shared_dir):
        transitions.append(f"awaiting:{job.status}")

    def record_approve(job_id, shared_dir, *, approved_by, notes=""):
        # Capture the status the job MUST be in at this point
        from evolve_admin.applications.forge_jobs import load_job
        # The fake_job is passed by reference; mark_awaiting_approval
        # should have flipped its status to "awaiting_approval"
        # before this is called.
        transitions.append(f"approve_called")

    job = _make_fake_job(job_type="install")
    with patch.object(forge_engine, "load_job", return_value=job), \
         patch.object(forge_engine, "_run_bot_dispatch"), \
         patch.object(forge_engine, "_run_integration_check"), \
         patch.object(forge_engine, "_resolve_api_key", return_value="sk-test"), \
         patch.object(forge_engine, "_get_models", return_value=("anthropic/claude-sonnet-4-6", "haiku")), \
         patch.object(forge_engine, "mark_awaiting_approval", side_effect=record_awaiting), \
         patch.object(forge_engine, "approve_forge_job", side_effect=record_approve), \
         patch.object(forge_engine, "_append_log"), \
         patch.object(forge_engine, "load_manifest", return_value=None), \
         patch.object(forge_engine, "save_manifest"), \
         patch("evolve_admin.applications.gallery.load_gallery_package",
               return_value=_fake_gallery_pkg()), \
         patch.object(forge_engine, "assemble_context_package", return_value={}), \
         patch.object(forge_engine, "_get_critique_rounds", return_value=2):
        forge_engine.run_forge_job(
            job_id="j-test1234",
            shared_dir=tmp_path,
            bot_id="test-bot",
            auto_approve_actor=None,
        )

    # mark_awaiting_approval ran BEFORE approve_forge_job
    assert "approve_called" in transitions
    awaiting_idx = next(
        i for i, t in enumerate(transitions) if t.startswith("awaiting:")
    )
    approve_idx = transitions.index("approve_called")
    assert awaiting_idx < approve_idx, (
        f"mark_awaiting_approval MUST run before approve_forge_job; "
        f"got transitions order: {transitions!r}"
    )


# ── Improvement jobs keep the gate ──────────────────────────────────────────


def test_improvement_job_keeps_operator_approval_gate(tmp_path):
    """Improvement jobs must NOT auto-ship. The operator needs to see
    the diff and decide. We verify this by asserting approve_forge_job
    is NEVER called for improvement jobs when no actor was passed
    in (i.e., the code takes the mark_awaiting_approval path)."""
    from evolve_admin.applications import forge_engine

    captured_approve_actor: list[str] = []
    captured_awaiting_calls: list = []

    def fake_approve(job_id, shared_dir, *, approved_by, notes=""):
        captured_approve_actor.append(approved_by)

    def fake_awaiting(job, shared_dir):
        captured_awaiting_calls.append(job)

    job = _make_fake_job(job_type="improvement")
    with patch.object(forge_engine, "load_job", return_value=job), \
         patch.object(forge_engine, "_run_bot_dispatch"), \
         patch.object(forge_engine, "_run_integration_check"), \
         patch.object(forge_engine, "_resolve_api_key", return_value="sk-test"), \
         patch.object(forge_engine, "_get_models", return_value=("anthropic/claude-sonnet-4-6", "haiku")), \
         patch.object(forge_engine, "approve_forge_job", side_effect=fake_approve), \
         patch.object(forge_engine, "mark_awaiting_approval", side_effect=fake_awaiting), \
         patch.object(forge_engine, "mark_step_running"), \
         patch.object(forge_engine, "mark_step_done"), \
         patch.object(forge_engine, "_notify_operator"), \
         patch.object(forge_engine, "_append_log"), \
         patch.object(forge_engine, "load_manifest", return_value=None), \
         patch.object(forge_engine, "assemble_context_package", return_value={}), \
         patch.object(forge_engine, "_get_critique_rounds", return_value=2):
        forge_engine.run_forge_job(
            job_id="j-test1234",
            shared_dir=tmp_path,
            bot_id="test-bot",
            auto_approve_actor=None,
        )

    # mark_awaiting_approval IS called for improvements
    assert len(captured_awaiting_calls) == 1
    # approve_forge_job is NOT called automatically — operator must act
    assert captured_approve_actor == []


# ── Caller-supplied actor wins ───────────────────────────────────────────────


def test_caller_supplied_auto_approve_actor_takes_precedence_for_install(tmp_path):
    """If a caller explicitly passes auto_approve_actor (e.g. evo's
    messaging-driven build flow), that value wins over the new install
    auto-trigger. The job records WHO approved it accurately."""
    from evolve_admin.applications import forge_engine

    captured_approve_actor: list[str] = []
    captured_approve_notes: list[str] = []

    def fake_approve(job_id, shared_dir, *, approved_by, notes=""):
        captured_approve_actor.append(approved_by)
        captured_approve_notes.append(notes)

    job = _make_fake_job(job_type="install")
    with patch.object(forge_engine, "load_job", return_value=job), \
         patch.object(forge_engine, "_run_bot_dispatch"), \
         patch.object(forge_engine, "_run_integration_check"), \
         patch.object(forge_engine, "_resolve_api_key", return_value="sk-test"), \
         patch.object(forge_engine, "_get_models", return_value=("anthropic/claude-sonnet-4-6", "haiku")), \
         patch.object(forge_engine, "approve_forge_job", side_effect=fake_approve), \
         patch.object(forge_engine, "_append_log"), \
         patch.object(forge_engine, "load_manifest", return_value=None), \
         patch.object(forge_engine, "save_manifest"), \
         patch("evolve_admin.applications.gallery.load_gallery_package",
               return_value=_fake_gallery_pkg()), \
         patch.object(forge_engine, "assemble_context_package", return_value={}), \
         patch.object(forge_engine, "_get_critique_rounds", return_value=2):
        forge_engine.run_forge_job(
            job_id="j-test1234",
            shared_dir=tmp_path,
            bot_id="test-bot",
            auto_approve_actor="evo_chat",  # explicit caller
        )

    assert captured_approve_actor == ["evo_chat"]
    # Notes reflect messaging-driven, not install-auto
    assert any("messaging-driven" in n for n in captured_approve_notes)
    assert all("install" not in n.lower() or "messaging" in n
               for n in captured_approve_notes)


def test_install_actor_only_set_when_actor_is_none(tmp_path):
    """Regression guard: the auto-set only triggers when the caller
    explicitly passed None, not on any other falsy value like an empty
    string.

    Empty-string passed by a caller is treated as "no auto-approve"
    (falls through to the regular awaiting_approval gate at Step 8).
    This is the safest interpretation: empty string is ambiguous, and
    we shouldn't silently rewrite it to a different actor sentinel
    that affects audit trails.
    """
    from evolve_admin.applications import forge_engine

    captured_approve_actor: list[str] = []
    captured_awaiting_calls: list = []

    def fake_approve(job_id, shared_dir, *, approved_by, notes=""):
        captured_approve_actor.append(approved_by)

    def fake_awaiting(job, shared_dir):
        captured_awaiting_calls.append(job)

    job = _make_fake_job(job_type="install")
    with patch.object(forge_engine, "load_job", return_value=job), \
         patch.object(forge_engine, "_run_bot_dispatch"), \
         patch.object(forge_engine, "_run_integration_check"), \
         patch.object(forge_engine, "_resolve_api_key", return_value="sk-test"), \
         patch.object(forge_engine, "_get_models", return_value=("anthropic/claude-sonnet-4-6", "haiku")), \
         patch.object(forge_engine, "approve_forge_job", side_effect=fake_approve), \
         patch.object(forge_engine, "mark_awaiting_approval", side_effect=fake_awaiting), \
         patch.object(forge_engine, "_notify_operator"), \
         patch.object(forge_engine, "_append_log"), \
         patch.object(forge_engine, "load_manifest", return_value=None), \
         patch.object(forge_engine, "save_manifest"), \
         patch("evolve_admin.applications.gallery.load_gallery_package",
               return_value=_fake_gallery_pkg()), \
         patch.object(forge_engine, "assemble_context_package", return_value={}), \
         patch.object(forge_engine, "_get_critique_rounds", return_value=2):
        forge_engine.run_forge_job(
            job_id="j-test1234",
            shared_dir=tmp_path,
            bot_id="test-bot",
            auto_approve_actor="",  # caller explicitly passed empty
        )

    # Empty string is treated as "no auto-approve" — regular gate runs
    assert captured_approve_actor == []
    assert len(captured_awaiting_calls) == 1


# ── Step 1 seed failure surfaces immediately (fresh-bot regression) ─────────


def test_step1_fails_hard_when_seed_save_raises(tmp_path):
    """REGRESSION: when the per-bot workspace/manifests/ directory doesn't
    exist (fresh bot, sudoers grants missing, or ACL stripped), save_manifest
    raises PermissionError. Pre-2026-06-01 the seed was buried in Step 2's
    _run_bot_dispatch and the exception was swallowed as 'non-fatal' — the
    forge then ran build + critique + test + auto-approve and only died at
    Step 10 with the misleading 'manifest not found' message.

    The fix moves the seed into Step 1 and treats save_manifest failures
    as fatal there. This test verifies:
      - The bot dispatch is NEVER reached when the seed can't persist
      - The job is marked failed at Step 1, not Step 10
      - approve_forge_job is never called (no wasted build work approved)
      - The error message mentions the bot_id and a remediation hint
    """
    from evolve_admin.applications import forge_engine

    bot_dispatch_calls: list = []
    approve_calls: list = []

    def fail_save_manifest(manifest, shared_dir):
        # Simulate the fresh-bot failure shape: PermissionError from the
        # sudo-cp fallback after the parent-dir mkdir also failed.
        raise PermissionError(
            "manifest parent mkdir failed (rc=1): "
            "sudo: a password is required"
        )

    def record_dispatch(*args, **kwargs):
        bot_dispatch_calls.append(args)

    def record_approve(*args, **kwargs):
        approve_calls.append((args, kwargs))

    job = _make_fake_job(job_type="install")
    with patch.object(forge_engine, "load_job", return_value=job), \
         patch.object(forge_engine, "_run_bot_dispatch", side_effect=record_dispatch), \
         patch.object(forge_engine, "_run_integration_check"), \
         patch.object(forge_engine, "_resolve_api_key", return_value="sk-test"), \
         patch.object(forge_engine, "_get_models", return_value=("anthropic/claude-sonnet-4-6", "haiku")), \
         patch.object(forge_engine, "approve_forge_job", side_effect=record_approve), \
         patch.object(forge_engine, "_append_log"), \
         patch.object(forge_engine, "load_manifest", return_value=None), \
         patch.object(forge_engine, "save_manifest", side_effect=fail_save_manifest), \
         patch("evolve_admin.applications.gallery.load_gallery_package",
               return_value=_fake_gallery_pkg()), \
         patch.object(forge_engine, "assemble_context_package", return_value={}), \
         patch.object(forge_engine, "_get_critique_rounds", return_value=2):
        forge_engine.run_forge_job(
            job_id="j-test1234",
            shared_dir=tmp_path,
            bot_id="test-bot",
            auto_approve_actor=None,
        )

    # The seed failed → run_forge_job returns at Step 1, never dispatching
    # the build and never invoking approve_forge_job.
    assert bot_dispatch_calls == [], (
        f"_run_bot_dispatch must NOT run after Step 1 seed failure; "
        f"got {len(bot_dispatch_calls)} call(s)"
    )
    assert approve_calls == [], (
        f"approve_forge_job must NOT run after Step 1 seed failure; "
        f"got {len(approve_calls)} call(s)"
    )

    # The job is left in a failed state with the actionable error
    assert job.status == "failed", f"expected failed, got {job.status!r}"
    step1 = next(s for s in job.steps if s.num == 1)
    assert step1.status == "failed"
    assert "could not seed manifest" in (step1.detail or "").lower()
    assert "test-bot" in (step1.detail or "")
    # Remediation hint pointing at the deploy fix
    assert "evolve-admin deploy" in (step1.detail or "")


def test_step1_fails_hard_when_gallery_package_missing(tmp_path):
    """REGRESSION: if pkg_id refers to a gallery package that doesn't exist
    (e.g. the imported gallery file was deleted between job creation and
    dispatch), Step 1 fails immediately — never dispatches to the bot.
    """
    from evolve_admin.applications import forge_engine

    bot_dispatch_calls: list = []
    approve_calls: list = []

    job = _make_fake_job(job_type="install")
    with patch.object(forge_engine, "load_job", return_value=job), \
         patch.object(forge_engine, "_run_bot_dispatch",
                      side_effect=lambda *a, **k: bot_dispatch_calls.append(a)), \
         patch.object(forge_engine, "_run_integration_check"), \
         patch.object(forge_engine, "_resolve_api_key", return_value="sk-test"), \
         patch.object(forge_engine, "_get_models", return_value=("anthropic/claude-sonnet-4-6", "haiku")), \
         patch.object(forge_engine, "approve_forge_job",
                      side_effect=lambda *a, **k: approve_calls.append((a, k))), \
         patch.object(forge_engine, "_append_log"), \
         patch.object(forge_engine, "load_manifest", return_value=None), \
         patch.object(forge_engine, "save_manifest"), \
         patch("evolve_admin.applications.gallery.load_gallery_package",
               return_value=None), \
         patch.object(forge_engine, "assemble_context_package", return_value={}), \
         patch.object(forge_engine, "_get_critique_rounds", return_value=2):
        forge_engine.run_forge_job(
            job_id="j-test1234",
            shared_dir=tmp_path,
            bot_id="test-bot",
            auto_approve_actor=None,
        )

    assert bot_dispatch_calls == []
    assert approve_calls == []
    assert job.status == "failed"
    step1 = next(s for s in job.steps if s.num == 1)
    assert step1.status == "failed"
    assert "gallery package" in (step1.detail or "").lower()
    assert "p-test1234" in (step1.detail or "")
