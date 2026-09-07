"""Evolve is the forge hash authority — bot sha256 claims are advisory.

Background (2026-07-11, Gate-2 gallery re-sweep on the mini): 4/4 direct
forge installs on macOS failed at 'Bot output verification' with sha256
mismatches on perfectly good output. Live outbox evidence showed two
failure shapes in the bot's self-reported ``files_written[].sha256``:

- placeholder invention — the bot couldn't run a hash command and wrote
  ``"sha256": "pending-exec-unavailable"`` (PM Inbox, Task Manager,
  Contacts);
- hash-then-edit — the bot hashed a file, then kept editing it, so the
  well-formed claim was stale against disk (Journal).

Both are self-report fragility, not build failures. And as an integrity
signal the claim is worthless: the same party writes the file and the
claim, so a lying bot would just claim the matching hash.

Fix: ``verify_files_on_disk`` recomputes sha256 from disk for every
declared path and returns records carrying the recomputed hash as the
authoritative value (flowing into ``build_manifest_file_records`` →
``manifest.files`` → Phase-C reality check / drift detection). The
bot's claim is compared advisorily — placeholder or mismatch produces a
warning, never a build failure. Existence (and unreadability) remain
hard failures.

This subsumes ``paths_rewritten_after`` (removed): the recovery-replay
scenario it existed for — ``recover_orphaned_jobs`` re-runs step 2 with
BUILD-era claims after a refine round rewrote the same files — now
resolves to an advisory warning instead of a false step-2 failure.
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


def _write_workspace_file(workspace: Path, rel: str, content: str = "x\n") -> dict:
    """Create a file under the workspace; return an entry with a CORRECT claim."""
    full = workspace / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    return {
        "path": rel,
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
        "file_id": "f-" + "0" * 8,
    }


# ── verify_files_on_disk: claim handling ─────────────────────────────────────


def test_matching_claim_verifies_clean(forge_dirs):
    bot_id, _, workspace = forge_dirs
    entry = _write_workspace_file(workspace, "scripts/ok.py", "hello\n")
    verified, warnings, errors = bot_forge.verify_files_on_disk(bot_id, [entry])
    assert errors == []
    assert warnings == []
    assert [e["path"] for e in verified] == ["scripts/ok.py"]
    assert verified[0]["sha256"] == entry["sha256"]


def test_placeholder_claim_proceeds_with_warning(forge_dirs):
    """The live j-30f0e2c0 shape: the bot wrote
    ``"sha256": "pending-exec-unavailable"`` for a file it built fine."""
    bot_id, _, workspace = forge_dirs
    content = "print('pm inbox')\n"
    entry = _write_workspace_file(workspace, "scripts/pm_inbox.py", content)
    entry["sha256"] = "pending-exec-unavailable"

    verified, warnings, errors = bot_forge.verify_files_on_disk(bot_id, [entry])
    assert errors == []
    assert len(warnings) == 1 and "not a hash" in warnings[0]
    # Recomputed hash is authoritative on the returned record.
    assert verified[0]["sha256"] == hashlib.sha256(content.encode()).hexdigest()


def test_stale_wellformed_claim_warns_not_fails(forge_dirs):
    """The live j-4acfef23 shape: the bot hashed, then edited the file —
    the claim is well-formed but stale. Disk content is the truth."""
    bot_id, _, workspace = forge_dirs
    final_content = "print('journal, post-edit')\n"
    entry = _write_workspace_file(workspace, "scripts/journal.py", final_content)
    stale = hashlib.sha256(b"pre-edit content\n").hexdigest()
    entry["sha256"] = stale

    verified, warnings, errors = bot_forge.verify_files_on_disk(bot_id, [entry])
    assert errors == []
    assert len(warnings) == 1
    assert stale[:8] in warnings[0]
    assert verified[0]["sha256"] == hashlib.sha256(
        final_content.encode()
    ).hexdigest()


def test_absent_claim_is_silent_existence_plus_recompute(forge_dirs):
    bot_id, _, workspace = forge_dirs
    content = "silent\n"
    entry = _write_workspace_file(workspace, "scripts/noclaim.py", content)
    del entry["sha256"]

    verified, warnings, errors = bot_forge.verify_files_on_disk(bot_id, [entry])
    assert errors == []
    assert warnings == []
    assert verified[0]["sha256"] == hashlib.sha256(content.encode()).hexdigest()


def test_missing_file_still_hard_fails(forge_dirs):
    bot_id, _, _ = forge_dirs
    entry = {"path": "scripts/never_written.py", "sha256": "0" * 64}
    verified, warnings, errors = bot_forge.verify_files_on_disk(bot_id, [entry])
    assert errors == ["missing on disk: scripts/never_written.py"]
    assert verified == []


def test_entry_with_no_path_still_hard_fails(forge_dirs):
    bot_id, _, _ = forge_dirs
    _, _, errors = bot_forge.verify_files_on_disk(bot_id, [{"sha256": "a" * 64}])
    assert errors == ["files_written entry has no path"]


def test_path_escaping_the_workspace_hard_fails(forge_dirs, tmp_path):
    """The recomputed hash is persisted to the bot-readable manifest, so
    hashing a path that resolves outside the workspace would hand the bot
    a content-confirmation oracle for files it can't read."""
    bot_id, _, workspace = forge_dirs
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("not yours\n")

    dotdot = {"path": "../outside-secret.txt", "sha256": "0" * 64}
    verified, _, errors = bot_forge.verify_files_on_disk(bot_id, [dotdot])
    assert verified == []
    assert errors == ["path escapes workspace: ../outside-secret.txt"]

    # Symlink inside the workspace pointing out is caught the same way.
    (workspace / "sneaky.txt").symlink_to(outside)
    verified, _, errors = bot_forge.verify_files_on_disk(
        bot_id, [{"path": "sneaky.txt"}],
    )
    assert verified == []
    assert errors == ["path escapes workspace: sneaky.txt"]


def test_verified_record_normalises_leading_slash_and_keeps_file_id(forge_dirs):
    bot_id, _, workspace = forge_dirs
    entry = _write_workspace_file(workspace, "scripts/x.py")
    entry["path"] = "/scripts/x.py"
    entry["file_id"] = "f-abc12345"
    verified, _, errors = bot_forge.verify_files_on_disk(bot_id, [entry])
    assert errors == []
    assert verified[0]["path"] == "scripts/x.py"
    assert verified[0]["file_id"] == "f-abc12345"


# ── build_manifest_file_records: recomputed hash lands downstream ────────────


def test_manifest_records_carry_the_recomputed_sha256(forge_dirs):
    bot_id, _, workspace = forge_dirs
    content = "record me\n"
    entry = _write_workspace_file(workspace, "scripts/rec.py", content)
    entry["sha256"] = "pending-recompute"  # placeholder claim

    verified, _, errors = bot_forge.verify_files_on_disk(bot_id, [entry])
    assert errors == []

    records = bot_forge.build_manifest_file_records(
        bot_id=bot_id,
        files_written=verified,
        app_id="rec-app",
        pkg_id="p-rec00001",
        run_id="r-00000001",
        now_iso_str="2026-07-11T00:00:00Z",
    )
    assert records[0]["path"] == "scripts/rec.py"
    assert records[0]["sha256"] == hashlib.sha256(content.encode()).hexdigest()


def test_manifest_records_preserve_classifier_layer_stamp(forge_dirs):
    """check_files_sha treats unknown-layer sha drift as MAJOR; a rebuild
    must not wipe a volatile (data/state) layer stamp off the record."""
    bot_id, _, workspace = forge_dirs
    entry = _write_workspace_file(workspace, "data/seen.json", "{}\n")
    verified, _, _ = bot_forge.verify_files_on_disk(bot_id, [entry])
    records = bot_forge.build_manifest_file_records(
        bot_id=bot_id,
        files_written=verified,
        app_id="a",
        pkg_id="p-x",
        run_id="r-2",
        now_iso_str="2026-07-11T00:00:00Z",
        existing=[{"path": "data/seen.json", "layer": "state",
                   "created_in_run": "r-1", "created_at": "2026-07-01T00:00:00Z"}],
    )
    assert records[0]["layer"] == "state"
    assert records[0]["created_in_run"] == "r-1"


def test_manifest_records_never_store_a_malformed_hash():
    """Defence for any caller that passes raw outbox claims instead of
    verified records: a non-64-hex value must not land on the manifest."""
    records = bot_forge.build_manifest_file_records(
        bot_id="personal-bot",
        files_written=[{"path": "scripts/x.py", "sha256": "claimed recomput…"}],
        app_id="a",
        pkg_id="p-x",
        run_id="r-1",
        now_iso_str="2026-07-11T00:00:00Z",
    )
    assert records[0]["sha256"] == ""


# ── Integration plumbing shared by the _run_bot_dispatch scenarios ───────────


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


def _wire_dispatch(monkeypatch, manifest, build_result, saved):
    monkeypatch.setattr(forge_engine, "load_manifest", lambda a, b, s: manifest)
    monkeypatch.setattr(
        forge_engine, "save_manifest", lambda m, s: saved.append(m),
    )
    monkeypatch.setattr(
        bot_forge, "dispatch_build", lambda b, r, **kw: build_result,
    )
    monkeypatch.setattr(
        bot_forge, "dispatch_critique",
        lambda b, r, **kw: bot_forge.CritiqueResult(
            status="complete", issues=[], notes="", raw={},
        ),
    )


def _build_result(entries: list[dict]) -> bot_forge.BuildResult:
    return bot_forge.BuildResult(
        status="complete",
        files_written=entries,
        test_run=None,
        test_exit_code=0,
        test_output="",
        notes="",
        raw={},
    )


# ── Integration: step 2 with advisory claims ─────────────────────────────────


def test_step2_passes_on_placeholder_claim_and_records_disk_hash(
    forge_dirs, monkeypatch, tmp_path,
):
    """End-to-end reproducer of the Gate-2 sweep failure: a placeholder
    claim on one file must not fail step 2, and manifest.files must end
    up carrying the evolve-recomputed hash."""
    bot_id, _, workspace = forge_dirs
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()

    content = "print('pm inbox')\n"
    entry = _write_workspace_file(workspace, "scripts/pm_inbox.py", content)
    entry["sha256"] = "pending-exec-unavailable"

    manifest = _manifest_for_integration()
    saved: list = []
    _wire_dispatch(monkeypatch, manifest, _build_result([entry]), saved)

    job = _job_for_integration()
    forge_engine._run_bot_dispatch(
        job, context={}, shared_dir=shared_dir, critique_rounds=1,
    )

    step2 = next(s for s in job.steps if s.num == 2)
    assert step2.status == "done", (
        f"step 2 should be 'done', got {step2.status!r} "
        f"(detail={step2.detail!r})"
    )
    rec = next(
        r for r in manifest.files if r["path"] == "scripts/pm_inbox.py"
    )
    assert rec["sha256"] == hashlib.sha256(content.encode()).hexdigest()


def test_step2_passes_on_stale_claim_no_refine_outbox(
    forge_dirs, monkeypatch, tmp_path,
):
    """A well-formed but stale claim (hash-then-edit) is advisory: step 2
    passes even with no later refine outbox to explain the divergence.
    This deliberately inverts the pre-2026-07-11 behaviour, which failed
    the build on any mismatch."""
    bot_id, forge_root, workspace = forge_dirs
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()

    rel = "apps/trip-research/research.py"
    final = b"post-edit content\n"
    full = workspace / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(final)
    entry = {
        "path": rel,
        "sha256": hashlib.sha256(b"pre-edit content\n").hexdigest(),
        "file_id": "f-build001",
    }
    _write_outbox(forge_root, "j-f7c19781", "",
                  {"status": "complete", "files_written": [entry]})

    manifest = _manifest_for_integration()
    saved: list = []
    _wire_dispatch(monkeypatch, manifest, _build_result([entry]), saved)

    job = _job_for_integration()
    forge_engine._run_bot_dispatch(
        job, context={}, shared_dir=shared_dir, critique_rounds=1,
    )

    step2 = next(s for s in job.steps if s.num == 2)
    assert step2.status == "done"
    rec = next(r for r in manifest.files if r["path"] == rel)
    assert rec["sha256"] == hashlib.sha256(final).hexdigest()
    # The divergence is visible in the job log as an advisory line.
    log_text = (
        shared_dir / "forge" / "logs" / "j-f7c19781.log"
    ).read_text()
    assert "advisory" in log_text


def test_step2_recovery_replay_after_refine_rewrote_files(
    forge_dirs, monkeypatch, tmp_path,
):
    """The scenario ``paths_rewritten_after`` existed for (j-f7c19781,
    2026-06-05/06): admin-ui restarted mid-refine; ``recover_orphaned_jobs``
    replays step 2; ``dispatch_build``'s resume-from-outbox returns
    BUILD-era claims while disk holds refine round 1's content. Must pass
    without the (now removed) outbox-walking skip machinery."""
    bot_id, forge_root, workspace = forge_dirs
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()

    rel = "apps/trip-research/research.py"
    refine_content = b"# refined by round 1\nprint('refined')\n"
    full = workspace / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(refine_content)
    refine_sha = hashlib.sha256(refine_content).hexdigest()

    build_entry = {
        "path": rel,
        "sha256": hashlib.sha256(b"BUILD CONTENT\n").hexdigest(),
        "file_id": "f-build001",
    }
    _write_outbox(forge_root, "j-f7c19781", "",
                  {"status": "complete", "files_written": [build_entry]})
    _write_outbox(forge_root, "j-f7c19781", "-r1",
                  {"status": "complete", "files_written": [
                      {"path": rel, "sha256": refine_sha,
                       "file_id": "f-build001"},
                  ]})

    manifest = _manifest_for_integration()
    saved: list = []
    _wire_dispatch(monkeypatch, manifest, _build_result([build_entry]), saved)

    job = _job_for_integration()
    forge_engine._run_bot_dispatch(
        job, context={}, shared_dir=shared_dir, critique_rounds=1,
    )

    step2 = next(s for s in job.steps if s.num == 2)
    assert step2.status == "done", (
        f"step 2 should be 'done', got {step2.status!r} "
        f"(detail={step2.detail!r})"
    )
    # The recomputed (refine-era) hash is what lands on the manifest.
    rec = next(r for r in manifest.files if r["path"] == rel)
    assert rec["sha256"] == refine_sha


def test_refine_round_records_recomputed_hash_despite_placeholder_claim(
    forge_dirs, monkeypatch, tmp_path,
):
    """The refine verify site gets the same authority treatment as the
    build site: a placeholder claim from the refine outbox must not fail
    the round, and manifest.files must carry the refine-era disk hash."""
    bot_id, _, workspace = forge_dirs
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()

    rel = "apps/trip-research/research.py"
    build_content = "print('build')\n"
    build_entry = _write_workspace_file(workspace, rel, build_content)

    refined_content = "print('refined')\n"

    def _fake_refine(bot, req, **kw):
        # The refine "rewrites" the file, then claims a placeholder hash.
        (workspace / rel).write_text(refined_content)
        return bot_forge.BuildResult(
            status="complete",
            files_written=[{"path": rel, "sha256": "claimed recomput…",
                            "file_id": build_entry["file_id"]}],
            test_run=None, test_exit_code=0, test_output="",
            notes="", raw={},
        )

    manifest = _manifest_for_integration()
    saved: list = []
    _wire_dispatch(monkeypatch, manifest, _build_result([build_entry]), saved)
    monkeypatch.setattr(
        bot_forge, "dispatch_critique",
        lambda b, r, **kw: bot_forge.CritiqueResult(
            status="complete",
            issues=[{"category": "edge-case", "description": "fix it",
                     "severity": "blocking"}],
            notes="", raw={},
        ),
    )
    monkeypatch.setattr(bot_forge, "dispatch_refine", _fake_refine)

    job = _job_for_integration()
    forge_engine._run_bot_dispatch(
        job, context={}, shared_dir=shared_dir, critique_rounds=1,
    )

    step4 = next(s for s in job.steps if s.num == 4)
    assert step4.status == "done", (
        f"refine step should be 'done', got {step4.status!r} "
        f"(detail={step4.detail!r})"
    )
    rec = next(r for r in manifest.files if r["path"] == rel)
    assert rec["sha256"] == hashlib.sha256(
        refined_content.encode()
    ).hexdigest()


def test_step2_still_fails_when_claimed_file_is_missing(
    forge_dirs, monkeypatch, tmp_path,
):
    """Existence stays a hard gate: a file the bot claims but never wrote
    fails step 2 exactly as before."""
    bot_id, _, workspace = forge_dirs
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()

    real = _write_workspace_file(workspace, "scripts/real.py")
    phantom = {
        "path": "scripts/never_written.py",
        "sha256": "0" * 64,
        "file_id": "f-phantom01",
    }

    manifest = _manifest_for_integration()
    saved: list = []
    _wire_dispatch(
        monkeypatch, manifest, _build_result([real, phantom]), saved,
    )

    with pytest.raises(RuntimeError, match="never_written.py"):
        forge_engine._run_bot_dispatch(
            _job_for_integration(), context={},
            shared_dir=shared_dir, critique_rounds=1,
        )
