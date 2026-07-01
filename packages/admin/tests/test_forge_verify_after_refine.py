"""Forge verification stays correct after refine has rewritten the same files.

Background (2026-06-05): a forge install job for a multi-file research
app failed at step 2 with sha256 mismatches on five .py files. The admin-ui had
restarted mid-refine; `recover_orphaned_jobs` re-invoked `run_forge_job`,
`dispatch_build`'s resume-from-outbox returned the original build outbox
(with BUILD-era hashes), and `verify_files_on_disk` then compared those
hashes against current disk content — which refine round 1 had legitimately
rewritten between the original build verify and the replay.

Every multi-critique-round forge install failed this way once admin
restarted mid-run.

Fix:
- ``paths_rewritten_after(bot_id, job_id, after_suffix)``: walks
  workspace outboxes whose lifecycle position is strictly after
  ``after_suffix`` (e.g. ``""`` → both ``-r1`` and ``-r2``; ``"-r1"`` →
  just ``-r2``) and returns the union of file paths they declared.
- ``verify_files_on_disk(..., skip_hash_for_paths=...)``: still requires
  existence but skips the sha256 check for paths a later outbox has
  rewritten.

Both wired into `_run_bot_dispatch`'s step-2 verify and the per-round
refine verify in ``forge_engine``.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import bot_forge, forge_engine  # noqa: E402
from evolve_admin.applications.forge_jobs import ForgeJob, ForgeStep  # noqa: E402
from evolve_admin.applications.manifest import (  # noqa: E402
    ApplicationManifest,
    MECHANISM_OC_HEARTBEAT_INSTRUCTION,
)


def _install_steps() -> list[ForgeStep]:
    return [
        ForgeStep(num=n, label=f"step {n}")
        for n in range(1, 11)
    ]


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def forge_dirs(tmp_path, monkeypatch):
    """Re-route ``bot_forge_dir`` and ``bot_workspace`` to tmp paths and
    pre-create inbox/outbox dirs."""
    bot_id = "personal-bot"
    forge_root = tmp_path / "forge"
    workspace = tmp_path / "workspace"
    (forge_root / "inbox").mkdir(parents=True)
    (forge_root / "outbox").mkdir(parents=True)
    workspace.mkdir(parents=True)

    monkeypatch.setattr(bot_forge, "bot_forge_dir", lambda b: forge_root)
    monkeypatch.setattr(bot_forge, "bot_workspace", lambda b: workspace)
    return bot_id, forge_root, workspace


def _write_outbox(forge_root: Path, job_id: str, suffix: str, payload: dict) -> Path:
    p = forge_root / "outbox" / f"{job_id}{suffix}.json"
    p.write_text(json.dumps(payload))
    return p


def _entry(path: str, content: str = "x\n") -> dict:
    return {
        "path": path,
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
        "file_id": "f-" + "0" * 8,
    }


def _write_workspace_file(workspace: Path, rel: str, content: str = "x\n") -> dict:
    full = workspace / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    return _entry(rel, content)


# ── paths_rewritten_after ─────────────────────────────────────────────────────


def test_paths_rewritten_after_empty_when_no_later_outboxes(forge_dirs):
    bot_id, forge_root, _ = forge_dirs
    _write_outbox(forge_root, "j-x", "", {"files_written": [_entry("a.py")]})
    assert bot_forge.paths_rewritten_after(bot_id, "j-x", after_suffix="") == set()


def test_paths_rewritten_after_collects_from_strictly_later_refine(forge_dirs):
    bot_id, forge_root, _ = forge_dirs
    _write_outbox(forge_root, "j-x", "", {"files_written": [_entry("a.py")]})
    _write_outbox(forge_root, "j-x", "-r1",
                  {"files_written": [_entry("a.py"), _entry("b.py")]})
    paths = bot_forge.paths_rewritten_after(bot_id, "j-x", after_suffix="")
    assert paths == {"a.py", "b.py"}


def test_paths_rewritten_after_unions_r1_and_r2_when_after_build(forge_dirs):
    bot_id, forge_root, _ = forge_dirs
    _write_outbox(forge_root, "j-x", "-r1", {"files_written": [_entry("a.py")]})
    _write_outbox(forge_root, "j-x", "-r2", {"files_written": [_entry("c.py")]})
    paths = bot_forge.paths_rewritten_after(bot_id, "j-x", after_suffix="")
    assert paths == {"a.py", "c.py"}


def test_paths_rewritten_after_excludes_self_and_earlier(forge_dirs):
    """``after_suffix='-r1'`` should yield only ``-r2``'s paths — neither
    the build outbox (earlier) nor ``-r1`` (self) counts."""
    bot_id, forge_root, _ = forge_dirs
    _write_outbox(forge_root, "j-x", "", {"files_written": [_entry("build_only.py")]})
    _write_outbox(forge_root, "j-x", "-r1",
                  {"files_written": [_entry("r1_only.py")]})
    _write_outbox(forge_root, "j-x", "-r2",
                  {"files_written": [_entry("r2_only.py")]})
    assert bot_forge.paths_rewritten_after(bot_id, "j-x", after_suffix="-r1") == {
        "r2_only.py"
    }


def test_paths_rewritten_after_ignores_critique_outboxes(forge_dirs):
    """Critique outboxes (-c1, -c2) carry ``issues``, not ``files_written``.
    They aren't part of the rewrite chain — even if a malformed one had a
    files_written list, the function would skip it because -c1/-c2 aren't
    in the lifecycle order."""
    bot_id, forge_root, _ = forge_dirs
    _write_outbox(forge_root, "j-x", "-c1",
                  {"files_written": [_entry("should_be_ignored.py")]})
    assert bot_forge.paths_rewritten_after(bot_id, "j-x", after_suffix="") == set()


def test_paths_rewritten_after_tolerates_malformed_outbox(forge_dirs):
    """Unreadable JSON in a later outbox must not crash verification."""
    bot_id, forge_root, _ = forge_dirs
    (forge_root / "outbox" / "j-x-r1.json").write_text("{ not valid json")
    _write_outbox(forge_root, "j-x", "-r2",
                  {"files_written": [_entry("ok.py")]})
    # -r1 is silently skipped; -r2 still contributes.
    assert bot_forge.paths_rewritten_after(bot_id, "j-x", after_suffix="") == {"ok.py"}


def test_paths_rewritten_after_strips_leading_slash(forge_dirs):
    bot_id, forge_root, _ = forge_dirs
    _write_outbox(forge_root, "j-x", "-r1",
                  {"files_written": [{"path": "/scripts/x.py", "sha256": "0" * 64}]})
    assert bot_forge.paths_rewritten_after(bot_id, "j-x", after_suffix="") == {
        "scripts/x.py"
    }


# ── verify_files_on_disk skip_hash_for_paths ─────────────────────────────────


def test_verify_skip_hash_for_paths_drops_mismatch_when_path_was_rewritten(
    forge_dirs,
):
    """The core regression: hash from build outbox doesn't match disk
    because refine rewrote the file. With the path in skip_hash_for_paths,
    verify should pass."""
    bot_id, _, workspace = forge_dirs
    # File exists at REFINE content but the outbox entry has BUILD's hash.
    refined_content = "from x import y\nprint('refined')\n"
    (workspace / "scripts").mkdir()
    (workspace / "scripts" / "research.py").write_text(refined_content)

    build_outbox_entry = {
        "path": "scripts/research.py",
        "sha256": hashlib.sha256(b"BUILD CONTENT\n").hexdigest(),
        "file_id": "f-build001",
    }

    # Without skip set: mismatch error.
    _, errors = bot_forge.verify_files_on_disk(bot_id, [build_outbox_entry])
    assert errors and "sha256 mismatch" in errors[0]

    # With skip set: passes, path is still verified (existence required).
    verified, errors = bot_forge.verify_files_on_disk(
        bot_id, [build_outbox_entry],
        skip_hash_for_paths={"scripts/research.py"},
    )
    assert verified == ["scripts/research.py"]
    assert errors == []


def test_verify_skip_hash_still_requires_existence(forge_dirs):
    """``skip_hash_for_paths`` is *not* a get-out-of-jail card for missing
    files. The path being skipped must still exist on disk; only the hash
    comparison is bypassed."""
    bot_id, _, _ = forge_dirs
    entry = {
        "path": "scripts/never_existed.py",
        "sha256": "0" * 64,
        "file_id": "f-phantom",
    }
    _, errors = bot_forge.verify_files_on_disk(
        bot_id, [entry], skip_hash_for_paths={"scripts/never_existed.py"},
    )
    assert errors == ["missing on disk: scripts/never_existed.py"]


def test_verify_skip_hash_none_keeps_pre_fix_behavior(forge_dirs):
    """Default (``skip_hash_for_paths=None``) is identical to old call —
    callers that don't opt in get strict hash checks."""
    bot_id, _, workspace = forge_dirs
    (workspace / "f.py").write_text("hello\n")
    entry = _entry("f.py", "hello\n")  # matches disk
    verified, errors = bot_forge.verify_files_on_disk(bot_id, [entry])
    assert verified == ["f.py"]
    assert errors == []


# ── Integration: recovery replay survives refine-modified files ──────────────


def _manifest_for_integration() -> ApplicationManifest:
    m = ApplicationManifest(id="trip-research", name="Trip Research", bot_id="personal-bot")
    m.scheduled_actions = [{
        "id": "trip-check",
        "mechanism": MECHANISM_OC_HEARTBEAT_INSTRUCTION,
        "install": {
            "file": "HEARTBEAT.md",
            "section_anchor": "## Trip Research — Check",
            "body": "...",
            "command": "python3 apps/trip-research/research.py check",
        },
    }]
    m.pkg_id = "p-tripresearch"
    m.pkg_version = "2026.06.05-1.0"
    m.build_spec = "Build a trip-research helper."
    return m


def _job_for_integration() -> ForgeJob:
    return ForgeJob(
        job_id="j-f7c19781",
        run_id="r-00000001",
        job_type="install",
        pkg_id="p-tripresearch",
        app_id="trip-research",
        bot_id="personal-bot",
        pkg_version_before=None,
        gallery_version="2026.06.05-1.0",
        steps=_install_steps(),
    )


def test_step2_verify_passes_when_refine_outbox_already_rewrote_the_file(
    forge_dirs, monkeypatch,
):
    """Recovery scenario reproducer (j-f7c19781, 2026-06-05/06):

    1. Original run: Build wrote ``apps/trip-research/research.py`` with hash
       BUILD_SHA. Critique round 1 flagged issues. Refine round 1 rewrote
       the file with hash REFINE_SHA (≠ BUILD_SHA). admin-ui restarted.
    2. ``recover_orphaned_jobs`` re-invokes ``run_forge_job`` from step 1.
    3. ``dispatch_build``'s resume-from-outbox returns the original build
       outbox (BUILD_SHA in files_written). The on-disk file has
       REFINE_SHA.

    Pre-fix: verify_files_on_disk raised sha256 mismatch on BUILD_SHA vs
    REFINE_SHA and step 2 was marked failed even though the build
    legitimately succeeded.

    Post-fix: ``paths_rewritten_after("j-...", after_suffix="")`` returns
    {"apps/trip-research/research.py"} from the refine outbox, so the hash
    check is skipped for that path; existence is still required and
    confirmed; step 2 passes.
    """
    bot_id, forge_root, workspace = forge_dirs
    shared_dir = workspace.parent / "shared"
    shared_dir.mkdir()

    # On-disk reality after refine round 1.
    refine_content = b"# refined by round 1\nprint('refined')\n"
    rel = "apps/trip-research/research.py"
    full = workspace / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(refine_content)
    refine_sha = hashlib.sha256(refine_content).hexdigest()

    # Build outbox carries BUILD-era hash; this is what dispatch_build's
    # resume-from-outbox would return on replay. We don't actually rely on
    # the resume path here — we mock dispatch_build to return the build
    # entry directly — but the refine outbox MUST exist on disk so
    # ``paths_rewritten_after`` finds it.
    build_sha = hashlib.sha256(b"BUILD CONTENT\n").hexdigest()
    build_entry = {"path": rel, "sha256": build_sha, "file_id": "f-build001"}
    _write_outbox(forge_root, "j-f7c19781", "",
                  {"status": "complete", "files_written": [build_entry]})

    # Refine round 1 outbox — declares it rewrote the same file with
    # REFINE_SHA. This is the signal the verifier uses to skip the build
    # hash check on this path.
    refine_entry = {"path": rel, "sha256": refine_sha, "file_id": "f-build001"}
    _write_outbox(forge_root, "j-f7c19781", "-r1",
                  {"status": "complete", "files_written": [refine_entry]})

    manifest = _manifest_for_integration()
    fake_build_result = bot_forge.BuildResult(
        status="complete",
        files_written=[build_entry],
        test_run="python3 -m py_compile apps/trip-research/research.py",
        test_exit_code=0,
        test_output="",
        notes="(resumed from prior outbox)",
        raw={},
        agent_exit_code=0,
        agent_stderr_tail="(resumed from prior outbox)",
    )

    monkeypatch.setattr(
        forge_engine, "load_manifest", lambda a, b, s: manifest,
    )
    monkeypatch.setattr(forge_engine, "save_manifest", lambda m, s: None)
    monkeypatch.setattr(
        bot_forge, "dispatch_build", lambda b, r, **kw: fake_build_result,
    )
    # Short-circuit critique → no issues → refine is skipped. The integration
    # is asserting the step-2 verify path, not the refine cycle itself.
    monkeypatch.setattr(
        bot_forge, "dispatch_critique",
        lambda b, r, **kw: bot_forge.CritiqueResult(
            status="complete", issues=[], notes="", raw={},
        ),
    )

    job = _job_for_integration()

    # Pre-fix this raised "Bot output verification failed: sha256 mismatch".
    forge_engine._run_bot_dispatch(
        job, context={}, shared_dir=shared_dir, critique_rounds=1,
    )

    # Step 2 should be marked done, not failed.
    step2 = next(s for s in job.steps if s.num == 2)
    assert step2.status == "done", (
        f"step 2 should be 'done', got {step2.status!r} "
        f"(detail={step2.detail!r})"
    )


def test_step2_verify_still_fails_on_mismatch_with_no_later_outbox(
    forge_dirs, monkeypatch,
):
    """Negative case: when no refine outbox exists (fresh first run), a
    real sha256 mismatch between the build outbox and disk content STILL
    fails. The skip is opt-in by lifecycle, not a blanket relaxation."""
    bot_id, forge_root, workspace = forge_dirs
    shared_dir = workspace.parent / "shared"
    shared_dir.mkdir()

    rel = "apps/trip-research/research.py"
    full = workspace / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    # On-disk content the bot didn't actually claim to write.
    full.write_bytes(b"unexpected content\n")

    build_entry = {
        "path": rel,
        "sha256": hashlib.sha256(b"BUILD CONTENT\n").hexdigest(),
        "file_id": "f-build001",
    }
    # Only the build outbox exists; no refine outbox.
    _write_outbox(forge_root, "j-f7c19781", "",
                  {"status": "complete", "files_written": [build_entry]})

    manifest = _manifest_for_integration()
    fake_build_result = bot_forge.BuildResult(
        status="complete",
        files_written=[build_entry],
        test_run=None, test_exit_code=0, test_output="", notes="", raw={},
    )

    monkeypatch.setattr(forge_engine, "load_manifest",
                        lambda a, b, s: manifest)
    monkeypatch.setattr(forge_engine, "save_manifest", lambda m, s: None)
    monkeypatch.setattr(bot_forge, "dispatch_build",
                        lambda b, r, **kw: fake_build_result)

    with pytest.raises(RuntimeError, match="sha256 mismatch"):
        forge_engine._run_bot_dispatch(
            _job_for_integration(), context={},
            shared_dir=shared_dir, critique_rounds=1,
        )
