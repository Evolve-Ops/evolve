"""Tests for evolve_admin.retire.

The retire flow is high-stakes (data archival + irreversible launchctl
operations). These tests exercise every step with mocked filesystems
and subprocess so the destructive paths run only against tmp_path and
fake launchctl outputs.

Test layout:

* The "summary" group covers Markdown generation from synthetic
  metric / proposal / signal / openclaw inputs.
* The "archive" group covers the copy-then-verify flow, including the
  silent-failure cases the spec flagged (missing source, manifest
  mismatch, STATUS.json updates).
* The "stop" group fakes subprocess so we can drive every branch of
  ``_stop_plist`` — including the load-bearing post-condition check
  where launchctl bootout returns 0 but the service is still loaded.
* The "full flow" group ties retire_bot end-to-end with all subprocess
  + sender hooks mocked.
* The "remove-evolve" group is the smaller-scope twin.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import retire  # noqa: E402
from evolve_admin.retire import (  # noqa: E402
    RetireResult,
    _build_manifest,
    _copy_tree_verified,
    _resolve_archive_root,
    _sha256_of_file,
    _stop_plist,
    _write_status,
    archive_bot_data,
    generate_closure_summary,
    notify_retirement,
    remove_evolve_plugin,
    remove_from_network,
    retire_bot,
    stop_bot_services,
)
from evolve_admin.runtime import LaunchdScheduler, set_scheduler  # noqa: E402


@pytest.fixture(autouse=True)
def _launchctl_via_retire_subprocess():
    """Route the Scheduler-seam launchctl calls through retire.subprocess.run.

    Post bite-4 migration, retire's macOS launchctl argv goes through the
    process-wide ``get_scheduler()`` / ``get_launchd_scheduler()`` seam (the
    module-global ``_probe_scheduler`` / ``_bootout_scheduler`` adapter
    instances are gone — the headline of this migration). The default seam
    runner spawns from the seam module, NOT from ``retire.subprocess.run``
    which every test here monkeypatches. This fixture injects a fake-runner
    ``LaunchdScheduler`` via ``set_scheduler()`` whose runner resolves
    ``retire.subprocess.run`` AT CALL TIME, so each test's existing
    ``monkeypatch.setattr(retire.subprocess, "run", fake)`` keeps driving the
    launchctl path exactly as before — and no test can ever reach a real
    ``launchctl`` (bootout is live-traffic destructive).

    The injected adapter is sudo-default (matching the bootout's posture and
    get_launchd_scheduler()); the probe helper derives an unsudo'd variant
    from it (reusing this runner because it is a custom runner). MANDATORY
    teardown: ``set_scheduler(None)`` — a leaked fake singleton poisons every
    later test that calls ``get_scheduler()``.

    macOS is pinned by the autouse conftest fixture, so every test below
    exercises the macOS bootout/probe/rm branch unless it flips the profile.
    """

    def _runner(argv):
        r = retire.subprocess.run(
            argv, capture_output=True, text=True, timeout=15,
        )
        return r.returncode, r.stdout or "", r.stderr or ""

    set_scheduler(LaunchdScheduler(runner=_runner))
    try:
        yield
    finally:
        set_scheduler(None)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _make_pod(tmp_path: Path, bot_id: str = "ghost", user: str = "ghost") -> dict:
    """Build a fake pod directory tree under tmp_path that mirrors the
    real layout the retire module expects:

      tmp_path/
        Users/<user>/.openclaw/openclaw.json
        Users/<user>/.openclaw/exec-approvals.json
        Users/<user>/.openclaw/workspace/note.md
        Shared/evolve/metrics/YYYY-MM-DD/<bot>.json
        Shared/evolve/proposals/{pending,applied,archived}/*.json
        Shared/evolve/signals/{firing,archived}/*.json
        Shared/evolve/network.json
    """
    home = tmp_path / "Users" / user
    oc_dir = home / ".openclaw"
    ws = oc_dir / "workspace"
    ws.mkdir(parents=True)
    (oc_dir / "openclaw.json").write_text(json.dumps({
        "agents": {"defaults": {
            "model": "claude-sonnet-4-5",
            "workspace": str(ws),
            "plugins": [{"id": "evolve"}, {"id": "github"}, {"id": "slack"}],
        }},
    }, indent=2))
    (oc_dir / "exec-approvals.json").write_text(json.dumps({
        "approved": [{"cmd": "ls"}, {"cmd": "git status"}],
    }))
    (oc_dir / "auth-profiles.json").write_text(json.dumps({"slack": {"team": "x"}}))
    (ws / "note.md").write_text("workspace evidence\n")

    shared = tmp_path / "Shared" / "evolve"
    shared.mkdir(parents=True)

    for date in ("2026-05-10", "2026-05-11", "2026-05-12"):
        _write_json(shared / "metrics" / date / f"{bot_id}.json", {
            "score": 78.5, "maintenance_ratio_7d": 0.12,
            "turns": 14, "cost_usd": 0.42,
            "default_model": "claude-sonnet-4-5",
        })
    # decoy metric for a different bot
    _write_json(shared / "metrics" / "2026-05-12" / "other.json",
                {"score": 80, "turns": 0})

    _write_json(shared / "proposals" / "applied" / "p-1.json", {
        "id": "p-1", "bot_id": bot_id, "title": "tighten exec approvals",
        "status": "applied_success", "generator": "exec_warden",
        "created_at": "2026-05-09T10:00:00Z",
    })
    _write_json(shared / "proposals" / "archived" / "p-2.json", {
        "id": "p-2", "bot_id": "other", "title": "decoy",
        "status": "dismissed",
    })

    _write_json(shared / "signals" / "firing" / "s-1.json", {
        "id": "s-1", "bot_id": bot_id, "producer": "host_health",
        "kind": "disk_high", "summary": "disk 92%",
        "state": "firing", "created_at": "2026-05-12T01:00:00Z",
    })
    _write_json(shared / "signals" / "archived" / "s-2.json", {
        "id": "s-2", "bot_id": "other", "producer": "audit", "summary": "decoy",
    })

    network = {
        "networkId": "test-pod",
        "primary": "primary_bot",
        "members": ["primary_bot", bot_id],
        "sharedDir": str(shared),
        "bots": {
            "primary_bot": {"role": "primary", "port": 19031},
            bot_id: {"role": "member", "port": 19032, "user": user},
        },
        "alerts": {"channel": "telegram", "chatId": "1234"},
    }
    _write_json(shared / "network.json", network)
    return {
        "home": home,
        "shared": shared,
        "network": network,
        "network_path": shared / "network.json",
        "bot_id": bot_id,
        "user": user,
    }


@pytest.fixture
def patch_bot_paths(monkeypatch):
    """Redirect retire.bot_home / get_bot_user to use the fake pod's home
    instead of the real /Users/<user>. Returns a closure that the
    individual tests configure with the home path."""
    state: dict[str, Path] = {}

    def _bot_home(bot_id: str, network: dict | None = None) -> Path:
        return state["home"]

    def _get_bot_user(bot_id: str, network: dict) -> str:
        # use whatever user is in network.json
        return (network.get("bots") or {}).get(bot_id, {}).get("user", bot_id)

    monkeypatch.setattr(retire, "bot_home", _bot_home)
    monkeypatch.setattr(retire, "get_bot_user", _get_bot_user)

    def _configure(home: Path) -> None:
        state["home"] = home

    return _configure


# ── Closure summary ──────────────────────────────────────────────────────────


def test_closure_summary_includes_identity_and_metrics(tmp_path, patch_bot_paths):
    pod = _make_pod(tmp_path)
    patch_bot_paths(pod["home"])

    md = generate_closure_summary(
        pod["bot_id"], pod["network"], pod["shared"],
        now=datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc),
    )

    # identity
    assert "# Closure summary — ghost" in md
    assert "**macOS user**: `ghost`" in md
    assert "**Role**: member" in md
    # capabilities
    assert "`evolve`" in md
    assert "`github`" in md
    assert "`slack`" in md
    assert "Auth profiles configured" in md
    assert "Exec approvals on file:** 2" in md
    # lifetime
    assert "2026-05-10" in md
    assert "2026-05-12" in md
    # recent activity table
    assert "| Date | Score | Maint % | Turns | Spend |" in md
    # proposals
    assert "p-1" in md
    assert "exec_warden" in md
    # signals
    assert "s-1" in md
    assert "disk 92%" in md


def test_closure_summary_handles_no_metrics(tmp_path, patch_bot_paths):
    pod = _make_pod(tmp_path, bot_id="nodata")
    patch_bot_paths(pod["home"])
    # remove all metrics
    import shutil
    shutil.rmtree(pod["shared"] / "metrics")

    md = generate_closure_summary(
        "nodata", pod["network"], pod["shared"],
        now=datetime(2026, 5, 12, tzinfo=timezone.utc),
    )
    assert "No metric history" in md
    assert "Conversation turns over scanned window: **0**" in md


def test_closure_summary_does_not_include_other_bots_proposals(
    tmp_path, patch_bot_paths,
):
    pod = _make_pod(tmp_path)
    patch_bot_paths(pod["home"])
    md = generate_closure_summary(
        pod["bot_id"], pod["network"], pod["shared"],
        now=datetime(2026, 5, 12, tzinfo=timezone.utc),
    )
    # p-2 belongs to a different bot
    assert "p-2" not in md
    assert "decoy" not in md


# ── Manifest + copy verification ─────────────────────────────────────────────


def test_sha256_of_file_matches_known(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"hello world\n")
    import hashlib
    expected = hashlib.sha256(b"hello world\n").hexdigest()
    assert _sha256_of_file(p) == expected


def test_build_manifest_excludes_self(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("bb")
    (tmp_path / "MANIFEST.json").write_text("{}")
    (tmp_path / "STATUS.json").write_text("{}")
    m = _build_manifest(tmp_path)
    paths = [f["path"] for f in m["files"]]
    assert "MANIFEST.json" not in paths
    assert "STATUS.json" not in paths
    assert "a.txt" in paths and "b.txt" in paths
    assert m["file_count"] == 2
    assert m["total_bytes"] == 3


def test_copy_tree_verified_succeeds_on_clean_copy(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hello")
    (src / "sub").mkdir()
    (src / "sub" / "b.txt").write_text("world!")

    dst = tmp_path / "dst"
    ok, info = _copy_tree_verified(src, dst)
    assert ok, info
    assert (dst / "a.txt").read_text() == "hello"
    assert (dst / "sub" / "b.txt").read_text() == "world!"
    assert info["total"] == 2
    assert info["copied"] == 2
    assert info["missing_count"] == 0
    assert info["fatal_error"] is None


def test_copy_tree_verified_excludes_node_modules(tmp_path):
    """node_modules is regenerable bulk (~96% of a real archive) and must
    be excluded from the copy, while package.json / package-lock.json (the
    forensic 'what was installed' record) and everything else is kept. The
    excluded files must NOT be counted as 'missing' by the audit."""
    src = tmp_path / "src"
    (src / "npm" / "projects" / "plugin-x" / "node_modules" / "left-pad").mkdir(
        parents=True
    )
    (src / "npm" / "projects" / "plugin-x" / "node_modules" / "left-pad" / "index.js").write_text(
        "module.exports = 1;"
    )
    (src / "npm" / "projects" / "plugin-x" / "package.json").write_text("{}")
    (src / "npm" / "projects" / "plugin-x" / "package-lock.json").write_text("{}")
    (src / "workspace").mkdir()
    (src / "workspace" / "note.md").write_text("evidence")

    dst = tmp_path / "dst"
    ok, info = _copy_tree_verified(src, dst)
    assert ok, info
    # node_modules never copied …
    assert not (dst / "npm" / "projects" / "plugin-x" / "node_modules").exists()
    # … but the lock/manifest + workspace evidence are kept.
    assert (dst / "npm" / "projects" / "plugin-x" / "package.json").exists()
    assert (dst / "npm" / "projects" / "plugin-x" / "package-lock.json").exists()
    assert (dst / "workspace" / "note.md").read_text() == "evidence"
    # Audit accounts only the 3 kept files; excluded bulk is not 'missing'.
    assert info["total"] == 3
    assert info["copied"] == 3
    assert info["missing_count"] == 0
    assert info["fatal_error"] is None


def test_copy_tree_verified_rejects_existing_destination(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a").write_text("a")
    dst = tmp_path / "dst"
    dst.mkdir()  # already exists; should refuse
    ok, info = _copy_tree_verified(src, dst)
    assert not ok
    assert "already exists" in info["fatal_error"]


def test_copy_tree_verified_reports_missing_source(tmp_path):
    ok, info = _copy_tree_verified(tmp_path / "no-such", tmp_path / "dst")
    assert not ok
    assert "source does not exist" in info["fatal_error"]


def _patch_copytree_with_after_top(monkeypatch, after_top):
    """Wrap shutil.copytree so ``after_top(src, dst)`` runs once, right
    after the outermost copytree call finishes. The wrapper handles
    shutil's internal recursion (which goes through the same module-
    level name and would otherwise re-trigger ``after_top`` on every
    subdirectory entry)."""
    real_copytree = retire.shutil.copytree
    depth = [0]

    def wrapped(s, d, *args, **kwargs):
        depth[0] += 1
        try:
            return real_copytree(s, d, *args, **kwargs)
        finally:
            depth[0] -= 1
            if depth[0] == 0:
                after_top(Path(s), Path(d))

    monkeypatch.setattr(retire.shutil, "copytree", wrapped)


def test_copy_tree_verified_post_copy_drift_is_partial_success(
    tmp_path, monkeypatch,
):
    """2026-06-01 retire regression: on a 30k-file .openclaw tree
    the running bot kept writing to its workspace mid-archive, so the
    post-copy audit found files in src that aren't in dst. Old shape
    flipped ok=False and the whole archive was thrown away. New shape:
    the drift surfaces in the audit but ok stays True so the caller can
    proceed."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("a")
    (src / "b.txt").write_text("bb")
    dst = tmp_path / "dst"

    def drift(s, _d):
        # Simulate: bot wrote a new session file after copytree finished.
        (s / "session-live.log").write_text("hot")
        # And appended to an existing file (size drift).
        (s / "b.txt").write_text("bb-extended")

    _patch_copytree_with_after_top(monkeypatch, drift)

    ok, info = _copy_tree_verified(src, dst)
    assert ok, info
    assert info["total"] == 3       # a.txt + b.txt + session-live.log
    assert info["copied"] == 1      # only a.txt landed cleanly
    assert info["missing_count"] == 2
    reasons = {m["reason"] for m in info["missing"]}
    assert reasons == {"absent in dst", "size mismatch"}
    assert info["fatal_error"] is None


def test_copy_tree_verified_zero_survivors_is_catastrophic(
    tmp_path, monkeypatch,
):
    """Pathological guard: copytree says it succeeded but the audit
    sees 0/N files in dst (dest vanished, or src was wholly replaced).
    Treat as catastrophic — caller must refuse to advance."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("a")
    (src / "b.txt").write_text("b")
    dst = tmp_path / "dst"

    def evil(_s, d):
        # Destination vanishes after the outermost copy.
        import shutil as _sh
        _sh.rmtree(d)
        d.mkdir()  # leave bare dir so verification can still walk

    _patch_copytree_with_after_top(monkeypatch, evil)

    ok, info = _copy_tree_verified(src, dst)
    assert ok is False
    assert info["total"] == 2
    assert info["copied"] == 0
    assert "0/2" in info["fatal_error"]


# ── Sudo-cp fallback when shutil.copytree hits EACCES ───────────────────────
#
# Pre-2026-06-01 `_copy_tree_verified` returned False on the first
# PermissionError raised by shutil.copytree. That symptom hit the
# wizard's API path (admin-ui runs as evolve, which has ACL read on
# /Users/<bot>/.openclaw/ but not on every plugin runtime subdirectory
# created later — browser-automation/, etc.). The CLI deploy path
# runs as root so it never hit the issue. The fallback retries the
# whole tree via `sudo /bin/cp -R` and chowns the result to evolve.


def test_copy_tree_verified_falls_back_to_sudo_on_permission_error(
    tmp_path, monkeypatch,
):
    """When shutil.copytree raises PermissionError, _copy_tree_verified
    must retry via _sudo_cp_tree_fallback. If the fallback succeeds,
    the audit walks the (now root-copied) tree and returns ok=True.
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("a")
    (src / "b.txt").write_text("b")
    dst = tmp_path / "dst"

    # First call: simulate shutil.copytree hitting EACCES on a subdir.
    raised = {"n": 0}
    real_copytree = shutil.copytree

    def fail_then_let_real_run(s, d, **kw):
        raised["n"] += 1
        if raised["n"] == 1:
            raise PermissionError(
                13, "Permission denied",
                f"{src}/plugin-skills/browser-automation",
            )
        return real_copytree(s, d, **kw)

    monkeypatch.setattr(retire.shutil, "copytree", fail_then_let_real_run)

    # The sudo fallback just shells out — fake it to do the actual
    # copy via the real shutil.copytree (no sudo subprocess in tests).
    fallback_calls = []
    def fake_fallback(s, d):
        fallback_calls.append((s, d))
        # Do the copy here so the audit step finds the files.
        real_copytree(s, d, symlinks=False, ignore_dangling_symlinks=True)
        return True, ""

    monkeypatch.setattr(retire, "_sudo_cp_tree_fallback", fake_fallback)

    ok, info = _copy_tree_verified(src, dst)
    assert ok is True, (
        f"sudo fallback should make _copy_tree_verified succeed on "
        f"permission errors. info: {info}"
    )
    assert len(fallback_calls) == 1, (
        f"fallback should be called exactly once; got {len(fallback_calls)}"
    )
    assert info["total"] == 2
    assert info["copied"] == 2


def test_copy_tree_verified_returns_fatal_when_both_paths_fail(
    tmp_path, monkeypatch,
):
    """If shutil.copytree raises AND the sudo fallback also fails,
    _copy_tree_verified returns ok=False with a fatal_error that
    surfaces BOTH failure detail strings — the operator needs to see
    what went wrong on each tier."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("a")
    dst = tmp_path / "dst"

    def always_fail(s, d, **kw):
        raise PermissionError(13, "Permission denied", "subdir")

    monkeypatch.setattr(retire.shutil, "copytree", always_fail)
    monkeypatch.setattr(
        retire, "_sudo_cp_tree_fallback",
        lambda s, d: (False, "sudo: a password is required"),
    )

    ok, info = _copy_tree_verified(src, dst)
    assert ok is False
    err = info["fatal_error"]
    assert "Permission denied" in err
    assert "password is required" in err, (
        f"Both failure details must surface in the operator-visible "
        f"error. Got: {err!r}"
    )


def test_copy_tree_verified_no_sudo_fallback_on_clean_copy(
    tmp_path, monkeypatch,
):
    """Happy path: direct shutil.copytree succeeds. _sudo_cp_tree_fallback
    must NOT be called — would add unnecessary subprocess overhead to
    every archive operation."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("a")
    dst = tmp_path / "dst"

    fallback_calls = []
    monkeypatch.setattr(
        retire, "_sudo_cp_tree_fallback",
        lambda s, d: (fallback_calls.append((s, d)), (True, ""))[1],
    )
    ok, _info = _copy_tree_verified(src, dst)
    assert ok is True
    assert fallback_calls == [], (
        f"Fallback must not run on the happy path; got {fallback_calls}"
    )


# ── Archive flow ─────────────────────────────────────────────────────────────


def test_resolve_archive_root_appends_suffix_on_collision(tmp_path):
    bot_id = "team_bot_a"
    now = datetime(2026, 5, 12, tzinfo=timezone.utc)
    p1 = _resolve_archive_root(tmp_path, bot_id, now)
    p1.mkdir(parents=True)
    p2 = _resolve_archive_root(tmp_path, bot_id, now)
    assert p1 != p2
    assert p2.name.endswith("-2")


def test_archive_bot_data_dry_run_creates_nothing(tmp_path, patch_bot_paths):
    pod = _make_pod(tmp_path)
    patch_bot_paths(pod["home"])
    archive_root = pod["shared"] / "archived-bots" / "ghost-2026-05-12"
    result = RetireResult(bot_id="ghost", dry_run=True)

    ok, manifest = archive_bot_data(
        "ghost", pod["network"], pod["shared"], archive_root,
        dry_run=True, result=result,
    )
    assert ok
    assert manifest.get("dry_run") is True
    assert not archive_root.exists()
    assert any("dry-run" in s for s in result.steps)


def test_archive_bot_data_real_copies_openclaw_and_metrics(tmp_path, patch_bot_paths):
    pod = _make_pod(tmp_path)
    patch_bot_paths(pod["home"])
    archive_root = pod["shared"] / "archived-bots" / "ghost-2026-05-12"
    result = RetireResult(bot_id="ghost", dry_run=False)

    ok, manifest = archive_bot_data(
        "ghost", pod["network"], pod["shared"], archive_root,
        dry_run=False, result=result,
    )
    assert ok, result.errors
    # openclaw copy
    assert (archive_root / "openclaw" / "openclaw.json").exists()
    assert (archive_root / "openclaw" / "workspace" / "note.md").read_text() == "workspace evidence\n"
    # metrics slice (only ghost's files, no decoy)
    assert (archive_root / "metrics" / "2026-05-12" / "ghost.json").exists()
    assert not (archive_root / "metrics" / "2026-05-12" / "other.json").exists()
    # network snapshot
    snap = json.loads((archive_root / "network.json.snapshot").read_text())
    assert snap["networkId"] == "test-pod"
    # manifest
    assert manifest["file_count"] >= 4
    assert manifest["total_bytes"] > 0
    written = json.loads((archive_root / "MANIFEST.json").read_text())
    assert written["bot_id"] == "ghost"
    # STATUS.json final flip
    status = json.loads((archive_root / "STATUS.json").read_text())
    assert status["step"] == "archive_complete"
    assert status["success"] is True


def test_archive_bot_data_missing_openclaw_marks_failure(tmp_path, patch_bot_paths):
    pod = _make_pod(tmp_path)
    patch_bot_paths(pod["home"])
    # delete .openclaw entirely
    import shutil
    shutil.rmtree(pod["home"] / ".openclaw")

    archive_root = pod["shared"] / "archived-bots" / "ghost-2026-05-12"
    result = RetireResult(bot_id="ghost", dry_run=False)
    ok, manifest = archive_bot_data(
        "ghost", pod["network"], pod["shared"], archive_root,
        dry_run=False, result=result,
    )
    assert ok is False
    assert any("required source missing" in e for e in result.errors)


def test_archive_bot_data_running_bot_drift_completes_successfully(
    tmp_path, patch_bot_paths, monkeypatch,
):
    """2026-06-01 retire regression. The bot's gateway was still running
    when archive_bot_data ran. shutil.copytree finished fine, but by the
    time the post-copy audit walked .openclaw the bot had written a
    new session file. Old behaviour: ok=False, STATUS.json
    success=false, retire_bot bailed before stopping services or
    writing the closure summary. New behaviour: drift surfaces as a
    `partial:` log line, manifest['copied'] records the missing files,
    and the archive still flips to success=true so the retire flow
    can proceed."""
    pod = _make_pod(tmp_path)
    patch_bot_paths(pod["home"])
    archive_root = pod["shared"] / "archived-bots" / "ghost-2026-05-12"
    result = RetireResult(bot_id="ghost", dry_run=False)

    def drift(s, _d):
        # Bot wrote new files to its workspace after copytree finished.
        (s / "workspace" / "session-live.log").write_text("hot turn")
        (s / "sessions").mkdir(exist_ok=True)
        (s / "sessions" / "active.jsonl").write_text('{"t":1}\n')

    _patch_copytree_with_after_top(monkeypatch, drift)

    ok, manifest = archive_bot_data(
        "ghost", pod["network"], pod["shared"], archive_root,
        dry_run=False, result=result,
    )
    assert ok, result.errors

    # openclaw bulk landed
    assert (archive_root / "openclaw" / "openclaw.json").exists()
    assert (archive_root / "openclaw" / "workspace" / "note.md").read_text() == "workspace evidence\n"
    # The drift files are in src but not in dst — that's expected.
    assert not (archive_root / "openclaw" / "workspace" / "session-live.log").exists()
    assert not (archive_root / "openclaw" / "sessions" / "active.jsonl").exists()

    # Manifest's per-label `copied` audit records the drift.
    oc_entry = next(c for c in manifest["copied"] if c["label"] == "openclaw")
    assert oc_entry["ok"] is True
    assert oc_entry["missing_count"] == 2
    assert oc_entry["copied"] == oc_entry["total"] - 2
    drift_paths = {m["path"] for m in oc_entry["missing"]}
    assert "workspace/session-live.log" in drift_paths
    assert "sessions/active.jsonl" in drift_paths

    # result.steps surfaces a partial: line so the operator sees it.
    assert any("partial" in s and "drifted" in s for s in result.steps), result.steps

    # STATUS.json final flip is success=true so retire_bot proceeds.
    status = json.loads((archive_root / "STATUS.json").read_text())
    assert status["step"] == "archive_complete"
    assert status["success"] is True


def test_retire_bot_drift_proceeds_through_full_flow(
    tmp_path, patch_bot_paths, monkeypatch,
):
    """End-to-end counterpart to the 2026-06-01 retire false-failure:
    drift during the archive must not block stop-services or the
    network.json removal. The closure summary must land, services must
    stop, and the bot must come out of network.json."""
    pod = _make_pod(tmp_path)
    patch_bot_paths(pod["home"])

    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    monkeypatch.setattr(retire, "LAUNCHD_DIR", launchd)
    fake = _FakeSubprocess()
    monkeypatch.setattr(retire.subprocess, "run",
                        _all_launchctl_not_loaded_subprocess(fake))

    def drift(s, _d):
        (s / "workspace" / "live.log").write_text("turn-in-flight")

    _patch_copytree_with_after_top(monkeypatch, drift)

    class _Outcome:
        result = "sent"

    result = retire_bot(
        pod["bot_id"], network_path=pod["network_path"],
        shared_dir=pod["shared"], dry_run=False,
        now=datetime(2026, 5, 12, tzinfo=timezone.utc),
        sender=lambda **kw: _Outcome(),
    )
    assert result.success, result.errors
    arc = result.archive_path
    assert arc is not None and arc.exists()
    # Closure summary written despite the drift.
    assert (arc / "CLOSURE_SUMMARY.md").exists()
    # Bot removed from network.json (services stopped path was reached).
    net = json.loads(pod["network_path"].read_text())
    assert "ghost" not in net["members"]
    # Final STATUS flip records success.
    status = json.loads((arc / "STATUS.json").read_text())
    assert status["step"] == "retire_complete"
    assert status["success"] is True


def test_write_status_is_atomic(tmp_path):
    _write_status(tmp_path, {"step": "x", "success": False})
    assert (tmp_path / "STATUS.json").exists()
    # second write replaces atomically; no .tmp leftover
    _write_status(tmp_path, {"step": "y", "success": True})
    assert json.loads((tmp_path / "STATUS.json").read_text())["step"] == "y"
    assert not (tmp_path / "STATUS.json.tmp").exists()


# ── Cleanup-on-failure (2026-06-01 same-day-retry disk-fillup) ──────────────


def test_archive_failure_populates_status_errors_and_cleans_up_bulk(
    tmp_path, patch_bot_paths,
):
    """2026-06-01 same-day-retry disk-fillup. The operator saw STATUS.json
    `success: false` with an empty `errors[]` — no clue what went wrong —
    while a 345 MB partial archive sat on disk consuming space. Two
    fixes flow together:

      1. STATUS.errors[] is populated from RetireResult.errors so the
         failure reason is inspectable.
      2. The bulk file content (openclaw/, metrics/, MANIFEST.json,
         network snapshot, closure summary) is removed on failure —
         keeping only STATUS.json (~1 KB) so the next retry doesn't
         stack another ghost 345 MB.
    """
    pod = _make_pod(tmp_path)
    patch_bot_paths(pod["home"])
    archive_root = pod["shared"] / "archived-bots" / "ghost-2026-05-12"

    # Force `_build_manifest` to populate manifest then make the
    # manifest write fail — gives us a deterministic ok_overall=False
    # late in archive_bot_data with bulk content already on disk.
    real_write_text = Path.write_text

    def fail_for_manifest(self, *args, **kwargs):
        if self.name == "MANIFEST.json":
            raise OSError(28, "no space left on device")
        return real_write_text(self, *args, **kwargs)

    # Patch Path.write_text via monkeypatch on the class object. Use
    # pytest's monkeypatch through the fixture by routing through a
    # tiny helper that catches the manifest path.
    import unittest.mock as _mock
    with _mock.patch.object(Path, "write_text", fail_for_manifest):
        result = RetireResult(bot_id="ghost", dry_run=False)
        ok, _manifest = archive_bot_data(
            "ghost", pod["network"], pod["shared"], archive_root,
            dry_run=False, result=result,
        )

    # Archive itself reports failure.
    assert ok is False
    assert any("write manifest" in e for e in result.errors), result.errors

    # STATUS.json populated with the failure detail.
    status = json.loads((archive_root / "STATUS.json").read_text())
    assert status["step"] == "archive_complete"
    assert status["success"] is False
    assert isinstance(status.get("errors"), list)
    assert status["errors"], "STATUS.errors must surface failure reason"
    assert any("write manifest" in e for e in status["errors"])
    # `steps` trail also surfaces so the operator can see what got
    # through (openclaw copy, metric snapshot, ...) before the failure.
    assert isinstance(status.get("steps"), list)

    # Bulk file content was cleaned up. STATUS.json survives.
    assert (archive_root / "STATUS.json").exists()
    assert not (archive_root / "openclaw").exists()
    assert not (archive_root / "metrics").exists()
    assert not (archive_root / "MANIFEST.json").exists()
    assert not (archive_root / "network.json.snapshot").exists()
    assert any(
        "cleanup-on-failure" in s for s in result.steps
    ), result.steps


def test_archive_success_does_not_trigger_cleanup(tmp_path, patch_bot_paths):
    """Mirror of the failure test. On success the bulk content stays on
    disk — we only nuke partial archives, never good ones."""
    pod = _make_pod(tmp_path)
    patch_bot_paths(pod["home"])
    archive_root = pod["shared"] / "archived-bots" / "ghost-2026-05-12"

    result = RetireResult(bot_id="ghost", dry_run=False)
    ok, _manifest = archive_bot_data(
        "ghost", pod["network"], pod["shared"], archive_root,
        dry_run=False, result=result,
    )
    assert ok, result.errors
    # STATUS records success — no errors[] populated.
    status = json.loads((archive_root / "STATUS.json").read_text())
    assert status["success"] is True
    assert "errors" not in status or status["errors"] == []
    # Bulk content preserved.
    assert (archive_root / "openclaw" / "openclaw.json").exists()
    assert (archive_root / "MANIFEST.json").exists()
    assert (archive_root / "network.json.snapshot").exists()


def test_resolve_archive_root_reuses_freed_slot_after_cleanup(tmp_path):
    """After cleanup-on-failure removes the bulk content, the slot still
    contains STATUS.json — so a retry on the same day appends `-2`. This
    is the intentional shape: forensic STATUS persists between attempts
    (so the operator can see *why* the prior attempt failed), at a cost
    of ~1 KB per ghost slot vs. the pre-fix 345 MB. Six attempts in
    three hours go from 1.7 GB → 6 KB."""
    bot_id = "ghost"
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)

    # Attempt 1 — full path.
    p1 = _resolve_archive_root(tmp_path, bot_id, now)
    p1.mkdir(parents=True)
    _write_status(p1, {"step": "archive_complete", "success": False,
                       "errors": ["disk full"]})

    # Attempt 2 — increments to -2 (slot 1 still has STATUS.json scrap).
    p2 = _resolve_archive_root(tmp_path, bot_id, now)
    assert p2 != p1
    assert p2.name.endswith("-2")

    # Per-slot disk cost: just STATUS.json (~ < 1 KB).
    status_size = (p1 / "STATUS.json").stat().st_size
    assert status_size < 4096, (
        f"failed-archive scrap should be < 4KB; got {status_size}"
    )


def test_cleanup_partial_archive_handles_missing_archive(tmp_path):
    """Defensive: if archive_root doesn't exist (called twice, called
    on a never-created archive), don't raise."""
    from evolve_admin.retire import _cleanup_partial_archive
    nonexistent = tmp_path / "missing-archive-dir"
    result = RetireResult(bot_id="ghost", dry_run=False)
    # Should not raise.
    _cleanup_partial_archive(nonexistent, result)


def test_cleanup_partial_archive_falls_back_to_sudo_on_permission_error(
    tmp_path, monkeypatch,
):
    """When the sudo_cp_tree_fallback path produced root-owned content,
    a direct shutil.rmtree raises PermissionError. The cleanup helper
    must fall back to ``sudo /bin/rm -rf`` (covered by the existing
    sudoers grant ``/Users/Shared/evolve/archived-bots/*``)."""
    from evolve_admin.retire import _cleanup_partial_archive

    archive_root = tmp_path / "ghost-2026-06-01"
    archive_root.mkdir()
    openclaw = archive_root / "openclaw"
    openclaw.mkdir()
    (openclaw / "openclaw.json").write_text("{}")
    _write_status(archive_root, {"step": "archive_complete", "success": False})

    # Capture a fresh reference to the unpatched rmtree BEFORE patching,
    # so the sudo-rm test simulator can use it without recursing into
    # the failing patched version (`retire.shutil` IS the shutil module).
    _real_rmtree = shutil.rmtree

    # Force shutil.rmtree to raise PermissionError on the first call.
    raised = {"n": 0}
    def fail_rmtree(p):
        raised["n"] += 1
        raise PermissionError(13, "Permission denied", str(p))
    monkeypatch.setattr(retire.shutil, "rmtree", fail_rmtree)

    # Capture the sudo rm subprocess call; simulate success by
    # actually removing the dir from the test harness side via the
    # un-patched rmtree we captured above.
    sudo_calls: list[list[str]] = []
    def fake_run(args, **kwargs):
        sudo_calls.append(list(args))
        if args[:3] == ["sudo", "/bin/rm", "-rf"]:
            target = Path(args[3])
            if target.is_dir():
                _real_rmtree(target, ignore_errors=True)
            elif target.exists():
                try:
                    target.unlink()
                except OSError:
                    pass
            class _R:
                returncode = 0
                stdout = ""
                stderr = ""
            return _R()
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()
    monkeypatch.setattr(retire.subprocess, "run", fake_run)

    result = RetireResult(bot_id="ghost", dry_run=False)
    _cleanup_partial_archive(archive_root, result)

    # Sudo fallback was invoked at least once for the bulk path(s).
    assert any(
        c[:3] == ["sudo", "/bin/rm", "-rf"] and "openclaw" in c[3]
        for c in sudo_calls
    ), sudo_calls
    # STATUS.json survives the cleanup.
    assert (archive_root / "STATUS.json").exists()
    # openclaw bulk dir is gone (sudo fallback handled it).
    assert not openclaw.exists()


# ── Plist stop ───────────────────────────────────────────────────────────────


class _FakeSubprocess:
    """Drives subprocess.run for the retire module's launchctl path.

    Configurable per call: each command lookup returns the next queued
    CompletedProcess-like object. Default behaviour: success.
    """

    def __init__(self):
        self.calls: list[list[str]] = []
        self.responses: dict[tuple[str, ...], list[Any]] = {}

    def queue(self, cmd_key: tuple[str, ...], *responses: Any) -> None:
        self.responses[cmd_key] = list(responses)

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        # Build a key from (executable name, *args)
        if "/launchctl" in args[0] or (
            len(args) > 1 and "/launchctl" in args[1]
        ):
            # ("launchctl", "print", "system/foo") or ("sudo", "/bin/launchctl", "bootout", "system/foo")
            launchctl_idx = 0 if "/launchctl" in args[0] else 1
            verb = args[launchctl_idx + 1]
            target = args[launchctl_idx + 2]
            key = ("launchctl", verb, target)
        elif args[0] == "sudo" and len(args) > 1 and args[1] == "/bin/rm":
            key = ("rm", args[2])
        else:
            key = tuple(str(a) for a in args)
        queue = self.responses.get(key)
        if queue is None:
            # default: success
            return _Cp(0, "", "")
        return queue.pop(0) if queue else _Cp(0, "", "")


class _Cp:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_stop_plist_dry_run_does_nothing(tmp_path, monkeypatch):
    fake = _FakeSubprocess()
    monkeypatch.setattr(retire.subprocess, "run", fake)
    # patch LAUNCHD_DIR so plist_path checks land inside tmp_path
    monkeypatch.setattr(retire, "LAUNCHD_DIR", tmp_path / "LaunchDaemons")

    result = RetireResult(bot_id="ghost", dry_run=True)
    ok = _stop_plist("ai.openclaw.ghost-gateway", dry_run=True, result=result)
    assert ok
    assert fake.calls == []
    assert any("dry-run" in s for s in result.steps)


def test_stop_plist_skips_when_not_present(tmp_path, monkeypatch):
    fake = _FakeSubprocess()
    # launchctl print returns non-zero → service not loaded
    fake.queue(("launchctl", "print", "system/ai.openclaw.ghost-gateway"),
               _Cp(1, "", "not found"))
    monkeypatch.setattr(retire.subprocess, "run", fake)
    monkeypatch.setattr(retire, "LAUNCHD_DIR", tmp_path / "LaunchDaemons")

    result = RetireResult(bot_id="ghost", dry_run=False)
    ok = _stop_plist("ai.openclaw.ghost-gateway", dry_run=False, result=result)
    assert ok
    assert any("already absent" in s for s in result.steps)


def test_stop_plist_bootout_success_removes_plist(tmp_path, monkeypatch):
    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    plist = launchd / "ai.openclaw.ghost-gateway.plist"
    plist.write_text("<plist/>")

    fake = _FakeSubprocess()
    # First print: loaded (rc=0)
    # bootout: success
    # Subsequent print after bootout: not loaded (rc=1) → success
    fake.queue(
        ("launchctl", "print", "system/ai.openclaw.ghost-gateway"),
        _Cp(0, "running", ""),   # initial probe — loaded
        _Cp(1, "", "not found"),  # post-bootout probe — gone
    )
    # rm physically deletes the file
    def _rm_handler(args, **kw):
        fake.calls.append(list(args))
        if args[:2] == ["sudo", "/bin/rm"]:
            try:
                Path(args[2]).unlink()
            except FileNotFoundError:
                pass
        return _Cp(0, "", "")

    # Wrap fake to also handle rm side-effect
    real_run = fake.__call__
    def _wrapped(args, **kw):
        if args[:2] == ["sudo", "/bin/rm"]:
            return _rm_handler(args, **kw)
        return real_run(args, **kw)
    monkeypatch.setattr(retire.subprocess, "run", _wrapped)
    monkeypatch.setattr(retire, "LAUNCHD_DIR", launchd)

    result = RetireResult(bot_id="ghost", dry_run=False)
    ok = _stop_plist("ai.openclaw.ghost-gateway", dry_run=False, result=result)
    assert ok, result.errors
    assert not plist.exists()
    assert any("stopped and removed" in s for s in result.steps)


# ── Linux branch: _stop_plist routes through get_scheduler().remove() ─────────


def test_stop_plist_linux_routes_through_systemd_remove(tmp_path):
    """On a Linux pod _stop_plist must adopt get_scheduler().remove() (the
    coordinator's audit-Q2 resolution) — NO plist, NO launchctl ``print``
    probe, NO separate sudo /bin/rm. The injected SystemdScheduler is driven
    via systemctl; not a single launchctl argv is emitted, and the unit file
    is deleted."""
    from platform_profile import LINUX, MACOS, set_profile
    from evolve_admin.runtime import SystemdScheduler, set_scheduler

    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    # The service unit must exist on disk for remove() to act on it.
    unit_file = unit_dir / "ai.openclaw.ghost-gateway.service"
    unit_file.write_text("[Service]\nExecStart=/bin/true\n")

    calls: list[list[str]] = []

    def _runner(argv):
        calls.append(list(argv))
        return 0, "", ""

    # use_sudo=False so the unit-file delete unlinks directly (no sudo rm
    # subprocess), keeping the recorded argv to systemctl verbs only.
    set_scheduler(
        SystemdScheduler(unit_dir=unit_dir, use_sudo=False, runner=_runner)
    )
    set_profile(LINUX)
    try:
        result = RetireResult(bot_id="ghost", dry_run=False)
        ok = _stop_plist("ai.openclaw.ghost-gateway", dry_run=False, result=result)
    finally:
        set_scheduler(None)
        set_profile(MACOS)

    assert ok, result.errors
    # The unit file was removed (the systemd analogue of the plist rm).
    assert not unit_file.exists()
    # systemctl disable --now + daemon-reload ran; ZERO launchctl calls.
    joined = [" ".join(c) for c in calls]
    assert any("disable" in j and "--now" in j for j in joined), joined
    assert any("daemon-reload" in j for j in joined), joined
    assert not any("launchctl" in j for j in joined), joined
    assert any("stopped and removed" in s for s in result.steps)


def test_stop_plist_linux_dry_run_does_not_call_remove(tmp_path):
    """Linux dry-run must be a pure no-op — remove() is never invoked (the
    injected runner records nothing) and the step is logged."""
    from platform_profile import LINUX, MACOS, set_profile
    from evolve_admin.runtime import SystemdScheduler, set_scheduler

    calls: list[list[str]] = []
    set_scheduler(
        SystemdScheduler(
            unit_dir=tmp_path, use_sudo=False,
            runner=lambda argv: (calls.append(list(argv)), (0, "", ""))[1],
        )
    )
    set_profile(LINUX)
    try:
        result = RetireResult(bot_id="ghost", dry_run=True)
        ok = _stop_plist("ai.openclaw.ghost-gateway", dry_run=True, result=result)
    finally:
        set_scheduler(None)
        set_profile(MACOS)

    assert ok
    assert calls == []
    assert any("dry-run" in s for s in result.steps)


def test_stop_plist_detects_still_loaded_after_bootout(tmp_path, monkeypatch):
    """The load-bearing silent-failure case: launchctl bootout returns 0
    but the service is somehow still loaded. We must detect this and
    surface an error rather than declaring success."""
    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    plist = launchd / "ai.openclaw.ghost-gateway.plist"
    plist.write_text("<plist/>")

    fake = _FakeSubprocess()
    fake.queue(
        ("launchctl", "print", "system/ai.openclaw.ghost-gateway"),
        _Cp(0, "running", ""),   # initial probe
        _Cp(0, "still running", ""),  # post-bootout #1 — still loaded
        _Cp(0, "still running", ""),  # post-bootout #2 (after sleep) — still loaded
    )
    monkeypatch.setattr(retire.subprocess, "run", fake)
    monkeypatch.setattr(retire, "LAUNCHD_DIR", launchd)
    # short-circuit the sleep so the test is fast
    monkeypatch.setattr(retire, "subprocess", retire.subprocess)
    import time
    monkeypatch.setattr(time, "sleep", lambda *a, **k: None)

    result = RetireResult(bot_id="ghost", dry_run=False)
    ok = _stop_plist("ai.openclaw.ghost-gateway", dry_run=False, result=result)
    assert ok is False
    assert any("still loaded" in e for e in result.errors)


def test_stop_plist_detects_rm_failure(tmp_path, monkeypatch):
    """If the plist file is still on disk after `rm`, treat as failure —
    a future launchctl bootstrap will pick it up again."""
    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    plist = launchd / "ai.openclaw.ghost-gateway.plist"
    plist.write_text("<plist/>")

    fake = _FakeSubprocess()
    fake.queue(
        ("launchctl", "print", "system/ai.openclaw.ghost-gateway"),
        _Cp(0, "running", ""),
        _Cp(1, "", "not found"),  # bootout succeeded
    )
    # rm returns 0 but does NOT delete the file
    def _wrapped(args, **kw):
        fake.calls.append(list(args))
        if args[:2] == ["sudo", "/bin/rm"]:
            return _Cp(0, "", "")
        return fake.__call__.__func__(fake, args, **kw)
    monkeypatch.setattr(retire.subprocess, "run", _wrapped)
    monkeypatch.setattr(retire, "LAUNCHD_DIR", launchd)

    result = RetireResult(bot_id="ghost", dry_run=False)
    ok = _stop_plist("ai.openclaw.ghost-gateway", dry_run=False, result=result)
    assert ok is False
    assert any("still exists" in e for e in result.errors)


def test_stop_plist_surfaces_rm_rc_and_stderr_when_sudo_denies(
    tmp_path, monkeypatch,
):
    """2026-06-01 retire incident root cause: sudoers grant for the rm of
    ``ai.openclaw.*-gateway.plist`` was missing. ``sudo /bin/rm`` failed
    with rc != 0 + a meaningful stderr ("Sorry, user evolve is not
    allowed to execute …"), but the subprocess result was discarded and
    the operator just saw "plist file still exists after rm". The error
    message must now include the rm rc + stderr so the next sudoers
    miss surfaces immediately."""
    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    plist = launchd / "ai.openclaw.ghost-gateway.plist"
    plist.write_text("<plist/>")

    fake = _FakeSubprocess()
    fake.queue(
        ("launchctl", "print", "system/ai.openclaw.ghost-gateway"),
        _Cp(0, "running", ""),
        _Cp(1, "", "not found"),  # bootout succeeded
    )

    def _wrapped(args, **kw):
        fake.calls.append(list(args))
        if args[:2] == ["sudo", "/bin/rm"]:
            # Simulate sudoers deny: rc != 0, stderr says why, file stays.
            return _Cp(
                1, "",
                "Sorry, user evolve is not allowed to execute "
                "'/bin/rm /Library/LaunchDaemons/"
                "ai.openclaw.ghost-gateway.plist' as root on this host.",
            )
        return fake.__call__.__func__(fake, args, **kw)
    monkeypatch.setattr(retire.subprocess, "run", _wrapped)
    monkeypatch.setattr(retire, "LAUNCHD_DIR", launchd)

    result = RetireResult(bot_id="ghost", dry_run=False)
    ok = _stop_plist("ai.openclaw.ghost-gateway", dry_run=False, result=result)
    assert ok is False
    err_text = "\n".join(result.errors)
    assert "still exists" in err_text
    # The actionable detail — rm rc + sudoers stderr — must surface.
    assert "rc=1" in err_text, err_text
    assert "not allowed to execute" in err_text, err_text


def test_stop_plist_surfaces_bootout_rc_when_still_loaded(
    tmp_path, monkeypatch,
):
    """If bootout fails with a non-zero rc + actionable stderr (e.g. a
    sudoers miss on the bootout pattern), the "still loaded" verifier
    error must include that detail so the operator sees the root cause
    rather than just the symptom."""
    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    plist = launchd / "ai.openclaw.ghost-gateway.plist"
    plist.write_text("<plist/>")

    fake = _FakeSubprocess()
    fake.queue(
        ("launchctl", "print", "system/ai.openclaw.ghost-gateway"),
        _Cp(0, "running", ""),   # initial probe
        _Cp(0, "still running", ""),  # post-bootout #1
        _Cp(0, "still running", ""),  # post-bootout #2 (after sleep)
    )

    def _wrapped(args, **kw):
        fake.calls.append(list(args))
        if args[:3] == ["sudo", "/bin/launchctl", "bootout"]:
            return _Cp(
                1, "",
                "Could not bootout system/ai.openclaw.ghost-gateway: "
                "5: Input/output error",
            )
        return fake.__call__.__func__(fake, args, **kw)
    monkeypatch.setattr(retire.subprocess, "run", _wrapped)
    monkeypatch.setattr(retire, "LAUNCHD_DIR", launchd)
    import time
    monkeypatch.setattr(time, "sleep", lambda *a, **k: None)

    result = RetireResult(bot_id="ghost", dry_run=False)
    ok = _stop_plist("ai.openclaw.ghost-gateway", dry_run=False, result=result)
    assert ok is False
    err_text = "\n".join(result.errors)
    assert "still loaded" in err_text
    assert "bootout rc=1" in err_text, err_text
    assert "Input/output error" in err_text, err_text


def test_sudoers_grants_rm_of_per_bot_gateway_plist():
    """2026-06-01 retire incident root cause regression guard. The retire
    flow's ``_stop_plist`` shells out to ``sudo /bin/rm
    /Library/LaunchDaemons/ai.openclaw.<bot>-gateway.plist``. Pre-fix
    the sudoers file granted rm only for ``ai.evolve.*`` and
    ``ai.openclaw.evolve.*``, so the rm of any per-bot gateway plist
    silently failed and the retire step reported "plist file still
    exists after rm". This guards against re-introducing that gap."""
    from evolve_admin.setup_wizard import _render_evolve_sudoers
    content = _render_evolve_sudoers()
    if content is None:
        pytest.skip("openclaw not discoverable; sudoers can't render")
    # The retire flow's exact rm shape — anywhere in the file.
    assert (
        "/bin/rm /Library/LaunchDaemons/ai.openclaw.*-gateway.plist"
        in content
    ), (
        "Missing sudoers grant for `/bin/rm "
        "/Library/LaunchDaemons/ai.openclaw.*-gateway.plist`. Without "
        "this, retire-bot's `_stop_plist` step fails on every per-bot "
        "gateway and the operator just sees 'plist file still exists "
        "after rm' (2026-06-01 retire 6-retry incident)."
    )


def test_stop_bot_services_iterates_all_per_bot_labels(tmp_path, monkeypatch):
    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    # No plists on disk → all are "already absent"
    fake = _FakeSubprocess()
    monkeypatch.setattr(retire.subprocess, "run", fake)
    monkeypatch.setattr(retire, "LAUNCHD_DIR", launchd)

    result = RetireResult(bot_id="ghost", dry_run=False)
    # Force every "launchctl print" to return rc=1 with the well-known
    # "not found" stderr (so the new permission-denied disambiguator
    # treats this as a clean unload rather than ambiguous failure).
    def _all_not_loaded(args, **kw):
        fake.calls.append(list(args))
        if "/launchctl" in args[0]:
            return _Cp(1, "", "Could not find service")
        return _Cp(0, "", "")
    monkeypatch.setattr(retire.subprocess, "run", _all_not_loaded)

    ok = stop_bot_services("ghost", network={}, dry_run=False, result=result)
    assert ok
    # Source-of-truth check: the set retire iterates over must equal
    # gateway + every label that ``deploy.per_bot_evolve_plist_labels``
    # installs. This prevents the C1.d pass-2 regression where retire's
    # daemon list drifted from the install set and ``evolve.test.{bot}``
    # + ``evolve.cost-converter.{bot}`` kept running after retirement.
    from evolve_admin.deploy import (
        per_bot_evolve_plist_labels,
        per_bot_gateway_plist_label,
    )
    expected = {per_bot_gateway_plist_label("ghost"), *per_bot_evolve_plist_labels("ghost")}
    assert set(result.plists_stopped) == expected


# ── network.json edits ───────────────────────────────────────────────────────


def test_remove_from_network_strips_bot(tmp_path):
    net_path = tmp_path / "network.json"
    _write_json(net_path, {
        "primary": "primary_bot",
        "members": ["primary_bot", "ghost"],
        "bots": {"primary_bot": {"port": 1}, "ghost": {"port": 2}},
        "sharedDir": str(tmp_path),
    })
    result = RetireResult(bot_id="ghost", dry_run=False)
    ok = remove_from_network("ghost", net_path, dry_run=False, result=result)
    assert ok
    data = json.loads(net_path.read_text())
    assert "ghost" not in data["members"]
    assert "ghost" not in data["bots"]
    assert "primary_bot" in data["members"]


def test_remove_from_network_refuses_primary(tmp_path):
    net_path = tmp_path / "network.json"
    _write_json(net_path, {
        "primary": "primary_bot",
        "members": ["primary_bot"],
        "bots": {"primary_bot": {"port": 1}},
        "sharedDir": str(tmp_path),
    })
    result = RetireResult(bot_id="primary_bot", dry_run=False)
    ok = remove_from_network("primary_bot", net_path, dry_run=False, result=result)
    assert ok is False
    assert any("primary" in e.lower() for e in result.errors)


def test_remove_from_network_dry_run_does_not_edit(tmp_path):
    net_path = tmp_path / "network.json"
    original = {
        "primary": "primary_bot",
        "members": ["primary_bot", "ghost"],
        "bots": {"primary_bot": {"port": 1}, "ghost": {"port": 2}},
        "sharedDir": str(tmp_path),
    }
    _write_json(net_path, original)
    result = RetireResult(bot_id="ghost", dry_run=True)
    ok = remove_from_network("ghost", net_path, dry_run=True, result=result)
    assert ok
    assert json.loads(net_path.read_text()) == original


# ── notification ─────────────────────────────────────────────────────────────


def test_notify_retirement_dry_run_does_not_send(tmp_path):
    result = RetireResult(bot_id="ghost", dry_run=True)
    sent: list[Any] = []
    def fake_send(**kw):
        sent.append(kw)
        return _Cp(0)
    notify_retirement(
        "ghost", tmp_path / "S.md", tmp_path / "arc", {}, tmp_path,
        dry_run=True, result=result, sender=fake_send,
    )
    assert sent == []
    assert result.notification_outcome == "dry_run"


def test_notify_retirement_passes_summary_path_to_dispatcher(tmp_path):
    result = RetireResult(bot_id="ghost", dry_run=False)
    captured: dict[str, Any] = {}
    class _Outcome:
        result = "sent"
    def fake_send(**kw):
        captured.update(kw)
        return _Outcome()
    notify_retirement(
        "ghost", tmp_path / "S.md", tmp_path / "arc",
        {"alerts": {"channel": "x"}}, tmp_path,
        dry_run=False, result=result, sender=fake_send,
    )
    assert captured["source"] == "retire"
    assert "ghost" in captured["message"]
    assert "S.md" in captured["message"]
    assert result.notification_outcome == "sent"


def test_notify_retirement_does_not_fail_overall_on_exception(tmp_path):
    """Notification failure is logged but must not flip result.success."""
    result = RetireResult(bot_id="ghost", dry_run=False)
    def bad_send(**kw):
        raise RuntimeError("telegram down")
    notify_retirement(
        "ghost", tmp_path / "S.md", tmp_path / "arc", {}, tmp_path,
        dry_run=False, result=result, sender=bad_send,
    )
    assert result.success is True
    assert result.notification_outcome == "exception"


# ── Full retire_bot flow ─────────────────────────────────────────────────────


def _all_launchctl_not_loaded_subprocess(fake: _FakeSubprocess):
    """Build a subprocess.run replacement that says every launchctl
    service is not loaded (so stop_bot_services succeeds without doing
    any real work)."""
    def _wrapped(args, **kw):
        fake.calls.append(list(args))
        a0 = args[0]
        if "/launchctl" in a0 or (len(args) > 1 and "/launchctl" in args[1]):
            launchctl_idx = 0 if "/launchctl" in a0 else 1
            verb = args[launchctl_idx + 1]
            if verb == "print":
                return _Cp(1, "", "not found")
            return _Cp(0, "", "")
        return _Cp(0, "", "")
    return _wrapped


def test_retire_bot_dry_run_creates_no_archive(tmp_path, patch_bot_paths, monkeypatch):
    pod = _make_pod(tmp_path)
    patch_bot_paths(pod["home"])

    sent: list[Any] = []
    def fake_send(**kw):
        sent.append(kw)
        return _Cp(0)

    result = retire_bot(
        pod["bot_id"], network_path=pod["network_path"],
        shared_dir=pod["shared"], dry_run=True,
        now=datetime(2026, 5, 12, tzinfo=timezone.utc),
        sender=fake_send,
    )
    assert result.success, result.errors
    assert result.dry_run is True
    # archived-bots dir is never created in dry-run
    assert not (pod["shared"] / "archived-bots").exists()
    # network.json is unchanged
    net = json.loads(pod["network_path"].read_text())
    assert pod["bot_id"] in net["members"]
    # no notification sent in dry-run
    assert sent == []


def test_retire_bot_full_flow_real(tmp_path, patch_bot_paths, monkeypatch):
    pod = _make_pod(tmp_path)
    patch_bot_paths(pod["home"])

    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    monkeypatch.setattr(retire, "LAUNCHD_DIR", launchd)
    fake = _FakeSubprocess()
    monkeypatch.setattr(retire.subprocess, "run",
                        _all_launchctl_not_loaded_subprocess(fake))
    sent: list[Any] = []
    class _Outcome:
        result = "sent"
    def fake_send(**kw):
        sent.append(kw)
        return _Outcome()

    result = retire_bot(
        pod["bot_id"], network_path=pod["network_path"],
        shared_dir=pod["shared"], dry_run=False,
        now=datetime(2026, 5, 12, tzinfo=timezone.utc),
        sender=fake_send,
    )

    assert result.success, result.errors
    # Archive exists
    arc = result.archive_path
    assert arc is not None and arc.exists()
    # Closure summary written
    summary = arc / "CLOSURE_SUMMARY.md"
    assert summary.exists()
    assert "# Closure summary — ghost" in summary.read_text()
    # Manifest present
    manifest = json.loads((arc / "MANIFEST.json").read_text())
    assert manifest["bot_id"] == "ghost"
    assert manifest["file_count"] > 0
    # STATUS.json final flip
    status = json.loads((arc / "STATUS.json").read_text())
    assert status["step"] == "retire_complete"
    assert status["success"] is True
    # network.json: bot stripped
    net = json.loads(pod["network_path"].read_text())
    assert "ghost" not in net["members"]
    assert "ghost" not in net["bots"]
    # primary preserved
    assert net["primary"] == "primary_bot"
    # Notification fired
    assert len(sent) == 1
    assert "ghost" in sent[0]["message"]


def test_retire_bot_refuses_unknown_bot(tmp_path, patch_bot_paths):
    pod = _make_pod(tmp_path)
    patch_bot_paths(pod["home"])
    result = retire_bot(
        "no-such-bot", network_path=pod["network_path"],
        shared_dir=pod["shared"], dry_run=False,
        sender=lambda **kw: _Cp(0),
    )
    assert result.success is False
    assert any("not in network.json" in e for e in result.errors)
    # No archive directory created
    assert not (pod["shared"] / "archived-bots").exists()


def test_retire_bot_refuses_primary(tmp_path, patch_bot_paths, monkeypatch):
    pod = _make_pod(tmp_path, bot_id="primary_bot", user="primary_bot")
    # The pod fixture sets primary to "primary_bot" by default, so the
    # bot we'll try to retire IS the primary. We need to point home at
    # primary_bot's openclaw dir.
    primary_home = tmp_path / "Users" / "primary_bot"
    oc = primary_home / ".openclaw"
    (oc / "workspace").mkdir(parents=True, exist_ok=True)
    _write_json(oc / "openclaw.json", {"agents": {"defaults": {"plugins": []}}})
    patch_bot_paths(primary_home)

    # Mock subprocess for any incidental launchctl calls
    fake = _FakeSubprocess()
    monkeypatch.setattr(retire.subprocess, "run",
                        _all_launchctl_not_loaded_subprocess(fake))
    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    monkeypatch.setattr(retire, "LAUNCHD_DIR", launchd)

    result = retire_bot(
        "primary_bot", network_path=pod["network_path"],
        shared_dir=pod["shared"], dry_run=False,
        sender=lambda **kw: _Cp(0),
    )
    # remove_from_network errors but archive completed; overall fail
    assert result.success is False
    assert any("primary" in e.lower() for e in result.errors)


def test_retire_bot_failed_archive_does_not_stop_services(
    tmp_path, patch_bot_paths, monkeypatch,
):
    """The paranoid invariant: if archive step does not verify clean,
    we MUST NOT proceed to launchctl bootout. The bot stays running
    so the operator can investigate."""
    pod = _make_pod(tmp_path)
    patch_bot_paths(pod["home"])
    # blow away the bot's .openclaw to force archive failure
    import shutil
    shutil.rmtree(pod["home"] / ".openclaw")

    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    monkeypatch.setattr(retire, "LAUNCHD_DIR", launchd)
    fake = _FakeSubprocess()
    monkeypatch.setattr(retire.subprocess, "run",
                        _all_launchctl_not_loaded_subprocess(fake))

    result = retire_bot(
        pod["bot_id"], network_path=pod["network_path"],
        shared_dir=pod["shared"], dry_run=False,
        sender=lambda **kw: _Cp(0),
    )
    assert result.success is False
    # No bootout/print attempted because we bailed before stop_bot_services
    launchctl_calls = [c for c in fake.calls if any("/launchctl" in str(a) for a in c)]
    assert launchctl_calls == []
    # Bot still in network.json
    net = json.loads(pod["network_path"].read_text())
    assert pod["bot_id"] in net["members"]


# ── remove-evolve flow ───────────────────────────────────────────────────────


def test_remove_evolve_plugin_keeps_gateway_running(
    tmp_path, patch_bot_paths, monkeypatch,
):
    pod = _make_pod(tmp_path)
    patch_bot_paths(pod["home"])

    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    # Pretend the gateway and every per-bot evolve plist are loaded.
    # Source the evolve list from the installer's source-of-truth so a
    # future deploy.py edit can't silently drift past this test.
    from evolve_admin.deploy import per_bot_evolve_plist_labels
    (launchd / "ai.openclaw.ghost-gateway.plist").write_text("<plist/>")
    for label in per_bot_evolve_plist_labels("ghost"):
        (launchd / f"{label}.plist").write_text("<plist/>")
    monkeypatch.setattr(retire, "LAUNCHD_DIR", launchd)

    # Track what plists were targeted. launchctl bootout for gateway
    # should NEVER be called.
    fake = _FakeSubprocess()
    booted_out: set[str] = set()
    def _wrapped(args, **kw):
        fake.calls.append(list(args))
        a0 = args[0]
        if "/launchctl" in a0 or (len(args) > 1 and "/launchctl" in args[1]):
            launchctl_idx = 0 if "/launchctl" in a0 else 1
            verb = args[launchctl_idx + 1]
            target = args[launchctl_idx + 2]
            if verb == "print":
                # After our bootout, say "not loaded" — include the
                # well-known "not found" marker so the probe's
                # permission-vs-not-loaded disambiguator treats this as
                # a real negative.
                if target in booted_out:
                    return _Cp(1, "", "Could not find service")
                return _Cp(0, "", "")
            if verb == "bootout":
                booted_out.add(target)
                return _Cp(0, "", "")
        if args[:2] == ["sudo", "/bin/rm"]:
            try:
                Path(args[2]).unlink()
            except FileNotFoundError:
                pass
            return _Cp(0, "", "")
        if args[:2] == ["sudo", "/bin/cp"]:
            # remove_evolve_plugin's openclaw.json rewrite stages a /tmp
            # file then sudo /bin/cp's it over the bot's openclaw.json.
            # Simulate the copy so the test's assertion on stripped
            # plugin entries can read the result.
            try:
                from shutil import copy2
                copy2(args[2], args[3])
            except Exception:
                pass
            return _Cp(0, "", "")
        if args[:2] == ["sudo", "/usr/sbin/chown"] or args[:2] == ["sudo", "/bin/chmod"]:
            return _Cp(0, "", "")
        return _Cp(0, "", "")
    monkeypatch.setattr(retire.subprocess, "run", _wrapped)

    result = remove_evolve_plugin(
        pod["bot_id"], network_path=pod["network_path"],
        shared_dir=pod["shared"], dry_run=False,
    )
    assert result.success, result.errors
    # Gateway plist still on disk; every per-bot evolve plist gone
    assert (launchd / "ai.openclaw.ghost-gateway.plist").exists()
    for label in per_bot_evolve_plist_labels("ghost"):
        assert not (launchd / f"{label}.plist").exists(), label
    # network.json: bot still there but flagged
    net = json.loads(pod["network_path"].read_text())
    assert pod["bot_id"] in net["members"]
    assert net["bots"][pod["bot_id"]]["evolve_disabled"] is True
    assert "evolve_disabled_at" in net["bots"][pod["bot_id"]]
    # Gateway-targeted bootout never happened
    gateway_bootout = [
        c for c in fake.calls
        if "bootout" in c and any("ghost-gateway" in str(a) for a in c)
    ]
    assert gateway_bootout == []


def test_remove_evolve_plugin_dry_run(tmp_path, patch_bot_paths, monkeypatch):
    pod = _make_pod(tmp_path)
    patch_bot_paths(pod["home"])
    monkeypatch.setattr(retire, "LAUNCHD_DIR", tmp_path / "LaunchDaemons")

    fake = _FakeSubprocess()
    monkeypatch.setattr(retire.subprocess, "run", fake)

    result = remove_evolve_plugin(
        pod["bot_id"], network_path=pod["network_path"],
        shared_dir=pod["shared"], dry_run=True,
    )
    assert result.success
    assert fake.calls == []  # dry-run never shells out
    # network.json unchanged
    net = json.loads(pod["network_path"].read_text())
    assert net["bots"][pod["bot_id"]].get("evolve_disabled") is None
    # openclaw.json unchanged in dry-run (the evolve plugin entry under
    # agents.defaults.plugins from _make_pod is still present)
    oc = json.loads(
        (pod["home"] / ".openclaw" / "openclaw.json").read_text()
    )
    plugins = oc.get("agents", {}).get("defaults", {}).get("plugins", [])
    plugin_ids = [
        p["id"] if isinstance(p, dict) else p for p in plugins
    ]
    assert "evolve" in plugin_ids, (
        "dry-run must not strip the evolve plugin from openclaw.json"
    )


# ── C1.d pass-2 regression tests ────────────────────────────────────────────
#
# These cover the three correctness gaps the Pass-2 reviewer flagged:
#   1. retire's per-bot daemon list drifted from deploy.py's install set
#   2. retire-bot / remove-evolve had no os.geteuid() root check, so the
#      unsudo'd launchctl probe silently misreported "service unloaded"
#      while bootout failed under non-root.
#   3. ``remove-evolve`` did not actually strip the evolve plugin from
#      openclaw.json — only stopped two daemons, leaving the runtime to
#      reload the plugin every turn.


def test_retire_per_bot_labels_match_deploy_source_of_truth():
    """retire._per_bot_plist_labels must equal gateway + every label
    deploy.per_bot_evolve_plist_labels installs. If anyone adds a new
    per-bot evolve daemon to deploy.py, retire-bot picks it up
    automatically — and if anyone forgets to update one side, this
    test breaks the build.
    """
    from evolve_admin.deploy import (
        per_bot_evolve_plist_labels,
        per_bot_gateway_plist_label,
    )
    from evolve_admin.retire import _per_bot_plist_labels

    labels = set(_per_bot_plist_labels("ghost"))
    expected = {per_bot_gateway_plist_label("ghost"), *per_bot_evolve_plist_labels("ghost")}
    # retire's set must be a superset (it can include legacy/defensive
    # entries discovered on disk, but must never miss an installed one).
    missing = expected - labels
    assert missing == set(), (
        f"retire-bot would leak {missing} after retirement — these are "
        "installed by deploy.py but not in retire's stop list"
    )


def test_retire_evolve_only_labels_match_deploy_source_of_truth():
    """Same check for the ``remove-evolve`` mode — must hit every
    Evolve-owned per-bot daemon, just without the gateway."""
    from evolve_admin.deploy import (
        per_bot_evolve_plist_labels,
        per_bot_gateway_plist_label,
    )
    from evolve_admin.retire import _evolve_only_plist_labels

    labels = set(_evolve_only_plist_labels("ghost"))
    expected = set(per_bot_evolve_plist_labels("ghost"))
    missing = expected - labels
    assert missing == set(), (
        f"remove-evolve would leak {missing} after disable"
    )
    # Gateway must NOT be in the evolve-only list — the bot keeps running.
    assert per_bot_gateway_plist_label("ghost") not in labels


def test_retire_bot_labels_include_legacy_measure_plist(tmp_path, monkeypatch):
    """If a legacy ``ai.openclaw.evolve.measure.<bot>.plist`` is still on
    disk (from before the pod-wide measure migration), retire-bot must
    pick it up via the LAUNCHD_DIR glob even though it isn't in the
    install set."""
    from evolve_admin.retire import _per_bot_plist_labels
    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    # Legacy per-bot measure plist
    (launchd / "ai.openclaw.evolve.measure.ghost.plist").write_text("<plist/>")
    monkeypatch.setattr(retire, "LAUNCHD_DIR", launchd)

    labels = _per_bot_plist_labels("ghost")
    assert "ai.openclaw.evolve.measure.ghost" in labels, (
        "legacy per-bot measure plist on disk must be enumerated for bootout "
        "even though deploy.py no longer installs it"
    )


def test_launchctl_probe_treats_permission_denied_as_still_loaded(monkeypatch):
    """The original bug: under a non-root caller, ``launchctl print
    system/<label>`` returns rc=1 with empty stderr, and the old code
    treated that as 'service unloaded'. After bootout (which silently
    fails without sudo), the same probe gave the same rc=1 → the flow
    declared green success while the daemon kept running.

    With the fix, the probe only returns False when the stderr/stdout
    matches a recognized 'not found' marker. An ambiguous failure
    (empty stderr, permission-denied stderr) returns True so the caller
    surfaces the failure.
    """
    from evolve_admin.retire import _launchctl_service_loaded

    class _Cp2:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    # Case 1: rc=1 with empty stderr — ambiguous (permission-denied
    # under non-root often produces this). Must report still loaded.
    monkeypatch.setattr(
        retire.subprocess, "run",
        lambda *a, **kw: _Cp2(1, "", ""),
    )
    assert _launchctl_service_loaded("anything") is True

    # Case 2: rc=1 with "Operation not permitted" stderr — must report
    # still loaded.
    monkeypatch.setattr(
        retire.subprocess, "run",
        lambda *a, **kw: _Cp2(1, "", "Operation not permitted"),
    )
    assert _launchctl_service_loaded("anything") is True

    # Case 3: rc=1 with the well-known "Could not find service ... on
    # domain" stderr — service genuinely not loaded.
    monkeypatch.setattr(
        retire.subprocess, "run",
        lambda *a, **kw: _Cp2(1, "", "Could not find service \"x\" on domain"),
    )
    assert _launchctl_service_loaded("anything") is False

    # Case 4: rc=0 — service is loaded.
    monkeypatch.setattr(
        retire.subprocess, "run",
        lambda *a, **kw: _Cp2(0, "running\n", ""),
    )
    assert _launchctl_service_loaded("anything") is True


def test_loaded_probe_argv_stays_unsudod():
    """The loaded-probe's UNSUDO'd posture is load-bearing — the tri-bucket
    classifier is calibrated on the unprivileged ``launchctl print`` output,
    and there is no ``print system/ai.openclaw.*`` sudoers grant for evolve.
    This pins the exact macOS argv so a future edit that silently flips the
    probe to ``sudo`` (which would change the privilege premise AND hit a
    sudoers miss) trips here. macOS is pinned by the autouse conftest."""
    from evolve_admin.retire import _launchctl_service_loaded
    from evolve_admin.runtime import LaunchdScheduler, set_scheduler

    recorded: list[list[str]] = []

    def _runner(argv):
        recorded.append(list(argv))
        return 1, "", "Could not find service on domain"  # → not loaded

    # Seam-default adapter is sudo-default (matches bootout/get_launchd_scheduler);
    # the probe helper must derive an UNSUDO'd variant from it.
    set_scheduler(LaunchdScheduler(runner=_runner))
    try:
        assert _launchctl_service_loaded("ai.openclaw.ghost-gateway") is False
    finally:
        set_scheduler(None)

    assert recorded == [
        ["/bin/launchctl", "print", "system/ai.openclaw.ghost-gateway"],
    ], recorded
    assert "sudo" not in recorded[0], "probe must stay unsudo'd (load-bearing)"


def _make_cli_runner(tmp_path):
    """Build a CliRunner + a minimal network.json under tmp_path. The
    tests pass --network-path so the CLI doesn't try to read a real pod.
    """
    from click.testing import CliRunner
    network_path = tmp_path / "network.json"
    _write_json(network_path, {
        "primary": "primary_bot",
        "members": ["primary_bot", "ghost"],
        "bots": {
            "primary_bot": {"role": "primary", "port": 19031},
            "ghost": {"role": "member", "port": 19032, "user": "ghost"},
        },
        "sharedDir": str(tmp_path),
    })
    return CliRunner(), network_path


def test_retire_bot_cli_requires_root_when_not_dry_run(tmp_path, monkeypatch):
    """Without sudo, retire-bot must bail with a 'run with sudo' message
    before invoking any retirement logic. Mirrors the pattern at
    cli.py:326, 710, 912, 1265.
    """
    from evolve_admin.cli import main as cli_main
    runner, network_path = _make_cli_runner(tmp_path)
    # Simulate non-root invocation
    monkeypatch.setattr(os, "geteuid", lambda: 501)
    # Anti-footgun: make retire_bot blow up if it gets called.
    from evolve_admin import retire as _retire_mod
    def _explode(*a, **kw):
        raise AssertionError("retire_bot must NOT run without root")
    monkeypatch.setattr(_retire_mod, "retire_bot", _explode)

    res = runner.invoke(cli_main, [
        "--network", str(network_path),
        "retire-bot", "ghost", "--yes",
    ])
    assert res.exit_code != 0, res.output
    assert "sudo" in res.output.lower()
    assert "retire-bot" in res.output


def test_retire_bot_cli_dry_run_allowed_without_root(tmp_path, monkeypatch):
    """dry-run skips the root gate — it never shells out, so it's safe
    to preview as the admin user.
    """
    import os as _os
    from evolve_admin.cli import main as cli_main
    runner, network_path = _make_cli_runner(tmp_path)
    monkeypatch.setattr(_os, "geteuid", lambda: 501)

    # Patch the function actually imported into cli's local namespace.
    called = {"n": 0}
    def _fake_retire_bot(bot_id, **kw):
        called["n"] += 1
        return retire.RetireResult(bot_id=bot_id, dry_run=True, success=True)
    monkeypatch.setattr(retire, "retire_bot", _fake_retire_bot)
    import evolve_admin.cli as _cli_mod
    monkeypatch.setattr(_cli_mod, "retire_bot", _fake_retire_bot, raising=False)

    res = runner.invoke(cli_main, [
        "--network", str(network_path),
        "retire-bot", "ghost", "--dry-run",
    ])
    # We just need the root-check NOT to fire; success/failure of the
    # downstream call is not the contract this test checks.
    assert "must be run with sudo" not in res.output.lower()


def test_remove_evolve_cli_requires_root_when_not_dry_run(tmp_path, monkeypatch):
    """Symmetric root-check test for the ``remove-evolve`` subcommand."""
    from evolve_admin.cli import main as cli_main
    runner, network_path = _make_cli_runner(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 501)

    from evolve_admin import retire as _retire_mod
    def _explode(*a, **kw):
        raise AssertionError("remove_evolve_plugin must NOT run without root")
    monkeypatch.setattr(_retire_mod, "remove_evolve_plugin", _explode)

    res = runner.invoke(cli_main, [
        "--network", str(network_path),
        "remove-evolve", "ghost", "--yes",
    ])
    assert res.exit_code != 0, res.output
    assert "sudo" in res.output.lower()
    assert "remove-evolve" in res.output


def test_remove_evolve_strips_plugin_from_openclaw_json(
    tmp_path, patch_bot_paths, monkeypatch,
):
    """The misleading-name fix: ``remove-evolve`` must actually edit the
    bot's openclaw.json to drop ``plugins.entries.evolve``,
    ``plugins.installs.evolve``, and any ``{"id": "evolve"}`` in the
    plugins list — otherwise the runtime keeps loading the plugin every
    turn after the daemons are stopped.
    """
    import os as _os
    pod = _make_pod(tmp_path)
    patch_bot_paths(pod["home"])

    # Rewrite the bot's openclaw.json to also include the entries/installs
    # nested form so we exercise every strip path.
    oc_path = pod["home"] / ".openclaw" / "openclaw.json"
    cfg = json.loads(oc_path.read_text())
    cfg["plugins"] = {
        "entries": {"evolve": {"config": {"foo": 1}}, "github": {"config": {}}},
        "installs": {"evolve": {"version": "1.0"}, "github": {"version": "2.0"}},
    }
    oc_path.write_text(json.dumps(cfg, indent=2))

    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    monkeypatch.setattr(retire, "LAUNCHD_DIR", launchd)

    fake = _FakeSubprocess()
    def _wrapped(args, **kw):
        fake.calls.append(list(args))
        a0 = args[0]
        if "/launchctl" in a0 or (len(args) > 1 and "/launchctl" in args[1]):
            idx = 0 if "/launchctl" in a0 else 1
            if args[idx + 1] == "print":
                return _Cp(1, "", "Could not find service")
            return _Cp(0, "", "")
        if args[:2] == ["sudo", "/bin/cp"]:
            # Simulate the strip rewrite: copy /tmp staging file over
            # the bot's openclaw.json so the post-condition assertion
            # below can read the result.
            try:
                from shutil import copy2
                copy2(args[2], args[3])
            except Exception:
                pass
            return _Cp(0, "", "")
        if args[:2] in (["sudo", "/usr/sbin/chown"], ["sudo", "/bin/chmod"], ["sudo", "/bin/rm"]):
            return _Cp(0, "", "")
        return _Cp(0, "", "")
    monkeypatch.setattr(retire.subprocess, "run", _wrapped)

    # As root for this test (we're testing the strip itself, not the CLI gate)
    monkeypatch.setattr(_os, "geteuid", lambda: 0)

    result = remove_evolve_plugin(
        pod["bot_id"], network_path=pod["network_path"],
        shared_dir=pod["shared"], dry_run=False,
    )
    assert result.success, result.errors

    # Re-read openclaw.json — both nested-form entries and the legacy
    # agents.defaults.plugins entry must be stripped.
    rewritten = json.loads(oc_path.read_text())
    assert "evolve" not in rewritten.get("plugins", {}).get("entries", {})
    assert "evolve" not in rewritten.get("plugins", {}).get("installs", {})
    # Other plugin entries preserved
    assert "github" in rewritten["plugins"]["entries"]
    assert "github" in rewritten["plugins"]["installs"]
    # Legacy agents.defaults.plugins list — evolve gone, others intact
    legacy = rewritten.get("agents", {}).get("defaults", {}).get("plugins", [])
    legacy_ids = [p["id"] if isinstance(p, dict) else p for p in legacy]
    assert "evolve" not in legacy_ids
    assert {"github", "slack"} <= set(legacy_ids)
    # Result steps reflect the rewrite
    assert any("stripped evolve plugin entries" in s for s in result.steps)


def test_remove_evolve_no_plugins_to_strip_is_clean_noop(
    tmp_path, patch_bot_paths, monkeypatch,
):
    """If openclaw.json doesn't contain any evolve plugin entries, the
    strip step logs a clean no-op and never invokes sudo /bin/cp. We
    must not error or rewrite the file."""
    import os as _os
    pod = _make_pod(tmp_path)
    patch_bot_paths(pod["home"])

    # Rewrite openclaw.json with NO evolve plugin entries anywhere.
    oc_path = pod["home"] / ".openclaw" / "openclaw.json"
    oc_path.write_text(json.dumps({
        "agents": {"defaults": {"plugins": [{"id": "github"}, {"id": "slack"}]}},
        "plugins": {"entries": {"github": {}}, "installs": {"github": {}}},
    }, indent=2))

    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    monkeypatch.setattr(retire, "LAUNCHD_DIR", launchd)

    fake = _FakeSubprocess()
    cp_calls: list[list] = []
    def _wrapped(args, **kw):
        fake.calls.append(list(args))
        a0 = args[0]
        if "/launchctl" in a0 or (len(args) > 1 and "/launchctl" in args[1]):
            idx = 0 if "/launchctl" in a0 else 1
            if args[idx + 1] == "print":
                return _Cp(1, "", "Could not find service")
            return _Cp(0, "", "")
        if args[:2] == ["sudo", "/bin/cp"]:
            cp_calls.append(list(args))
            return _Cp(0, "", "")
        return _Cp(0, "", "")
    monkeypatch.setattr(retire.subprocess, "run", _wrapped)
    monkeypatch.setattr(_os, "geteuid", lambda: 0)

    result = remove_evolve_plugin(
        pod["bot_id"], network_path=pod["network_path"],
        shared_dir=pod["shared"], dry_run=False,
    )
    assert result.success, result.errors
    # No openclaw.json sudo /bin/cp rewrite when nothing changed.
    oc_str = str(oc_path)
    rewrites = [c for c in cp_calls if oc_str in c]
    assert rewrites == [], rewrites
    assert any("nothing to strip" in s for s in result.steps)


# ── delete_bot (full nuke) ───────────────────────────────────────────────────
#
# delete_bot composes retire_bot + macOS user deletion. The tests below pin
# the three properties that matter operationally:
#
#   1. dscl is invoked exactly once (and only) when bot_user == bot_id —
#      the common case. Missing this invocation would make Delete behave
#      like Retire silently.
#   2. dscl is NEVER invoked when bot_user != bot_id (piggyback case
#      where the bot runs under an existing operator user account).
#      The wrong call here would nuke a real person's home directory;
#      this is the highest-stakes guard in the function.
#   3. dry-run never calls dscl — operators rely on dry-run to preview
#      destructive ops safely.
#
# All three use the same fake-subprocess pattern as the retire_bot tests.


def _delete_bot_subprocess_recorder(fake: _FakeSubprocess):
    """Returns a subprocess.run shim that:
      * routes launchctl through `fake` (so retire_bot's services step
        succeeds via the same "not loaded" path the retire tests use), and
      * records dscl / rm calls separately so the test can assert on them.
    """
    base = _all_launchctl_not_loaded_subprocess(fake)
    recorded = {"dscl": [], "rm_home": []}

    def _shim(args, **kwargs):
        # dscl . -delete /Users/<user>
        if len(args) >= 5 and args[0] == "sudo" and args[1] == "/usr/bin/dscl" \
                and args[3] == "-delete":
            recorded["dscl"].append(list(args))
            return _Cp(0, "", "")
        # rm -rf /Users/<user>  (only the home-directory delete from
        # _dscl_delete_user; the retire path uses /bin/rm with different
        # paths which we don't record here)
        if len(args) >= 4 and args[0] == "sudo" and args[1] == "/bin/rm" \
                and args[2] == "-rf" and args[3].startswith("/Users/"):
            recorded["rm_home"].append(list(args))
            return _Cp(0, "", "")
        return base(args, **kwargs)

    return _shim, recorded


def test_delete_bot_invokes_dscl_when_user_matches_bot_id(
    tmp_path, patch_bot_paths, monkeypatch,
):
    """Common case: wizard-provisioned bot (bot_id == macOS user).
    delete_bot should run the full retire flow AND call dscl + rm -rf."""
    pod = _make_pod(tmp_path, bot_id="ghost", user="ghost")
    patch_bot_paths(pod["home"])

    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    monkeypatch.setattr(retire, "LAUNCHD_DIR", launchd)

    fake = _FakeSubprocess()
    shim, recorded = _delete_bot_subprocess_recorder(fake)
    # Route both retire.subprocess.run AND provisioning.subprocess.run —
    # _dscl_delete_user lives in provisioning.py and calls subprocess.run
    # bound there, not the one bound in retire.
    monkeypatch.setattr(retire.subprocess, "run", shim)
    from evolve_admin import provisioning
    monkeypatch.setattr(provisioning.subprocess, "run", shim)

    result = retire.delete_bot(
        pod["bot_id"], network_path=pod["network_path"],
        shared_dir=pod["shared"], dry_run=False,
        now=datetime(2026, 5, 12, tzinfo=timezone.utc),
        sender=lambda **kw: type("_O", (), {"result": "sent"})(),
    )

    assert result.success, result.errors
    # Retire side-effects all happened
    assert result.archive_path is not None and result.archive_path.exists()
    net = json.loads(pod["network_path"].read_text())
    assert "ghost" not in net["members"]
    # dscl + rm called exactly once each, both targeting /Users/ghost
    assert len(recorded["dscl"]) == 1, recorded["dscl"]
    assert recorded["dscl"][0][4] == "/Users/ghost"
    assert len(recorded["rm_home"]) == 1, recorded["rm_home"]
    assert recorded["rm_home"][0][3] == "/Users/ghost"
    # And the result log explicitly says we did it
    assert any("deleted macOS user ghost" in s for s in result.steps)


def test_delete_bot_refuses_dscl_when_bot_user_differs_from_bot_id(
    tmp_path, patch_bot_paths, monkeypatch,
):
    """Piggyback case: a bot runs under a macOS user whose account name
    differs from the bot id (the real-world example from memory is a
    bot piggybacking on its operator's personal user account).
    delete_bot must retire the bot but MUST NOT touch the macOS user —
    that would nuke a real person's home directory."""
    pod = _make_pod(
        tmp_path, bot_id="piggyback_bot", user="shared_account",
    )
    patch_bot_paths(pod["home"])

    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    monkeypatch.setattr(retire, "LAUNCHD_DIR", launchd)

    fake = _FakeSubprocess()
    shim, recorded = _delete_bot_subprocess_recorder(fake)
    monkeypatch.setattr(retire.subprocess, "run", shim)
    from evolve_admin import provisioning
    monkeypatch.setattr(provisioning.subprocess, "run", shim)

    result = retire.delete_bot(
        pod["bot_id"], network_path=pod["network_path"],
        shared_dir=pod["shared"], dry_run=False,
        now=datetime(2026, 5, 12, tzinfo=timezone.utc),
        sender=lambda **kw: type("_O", (), {"result": "sent"})(),
    )

    assert result.success, result.errors
    # Bot was retired
    net = json.loads(pod["network_path"].read_text())
    assert "piggyback_bot" not in net["members"]
    # But dscl was NEVER called — this is the critical safety property
    assert recorded["dscl"] == [], (
        "delete_bot should NEVER dscl-delete when bot_user != bot_id; "
        "doing so would nuke a real person's home directory"
    )
    assert recorded["rm_home"] == [], recorded["rm_home"]
    # And the operator is told what happened + how to do it manually
    assert any("piggyback" in s.lower() for s in result.steps)
    assert any(
        "dscl . -delete /Users/shared_account" in s for s in result.steps
    )


def test_delete_bot_dry_run_never_calls_dscl(
    tmp_path, patch_bot_paths, monkeypatch,
):
    """Dry-run is the operator's safety net. It must not touch the
    macOS user account under any circumstance."""
    pod = _make_pod(tmp_path)
    patch_bot_paths(pod["home"])

    fake = _FakeSubprocess()
    shim, recorded = _delete_bot_subprocess_recorder(fake)
    monkeypatch.setattr(retire.subprocess, "run", shim)
    from evolve_admin import provisioning
    monkeypatch.setattr(provisioning.subprocess, "run", shim)

    result = retire.delete_bot(
        pod["bot_id"], network_path=pod["network_path"],
        shared_dir=pod["shared"], dry_run=True,
        now=datetime(2026, 5, 12, tzinfo=timezone.utc),
        sender=lambda **kw: type("_O", (), {"result": "sent"})(),
    )

    assert result.success, result.errors
    assert result.dry_run is True
    # No archive directory and no dscl/rm calls
    assert recorded["dscl"] == []
    assert recorded["rm_home"] == []
    net = json.loads(pod["network_path"].read_text())
    assert pod["bot_id"] in net["members"]
    # Result log mentions the dry-run preview
    assert any("[dry-run] would delete macOS user" in s for s in result.steps)


def test_delete_bot_refuses_truly_unknown_bot(
    tmp_path, patch_bot_paths, monkeypatch,
):
    """If the bot is not in network.json AND its home dir does not
    exist on disk, refuse before doing anything. The home-dir check
    distinguishes "completely unknown bot" (refuse) from "already
    retired but macOS user still present" (proceed to cleanup —
    see test_delete_bot_completes_cleanup_for_retired_bot below)."""
    pod = _make_pod(tmp_path)
    patch_bot_paths(pod["home"])
    # Pin the disk-check to False — the bot home does not exist.
    monkeypatch.setattr(
        retire, "_bot_home_exists_on_disk", lambda bot_id: False,
    )
    result = retire.delete_bot(
        "no-such-bot", network_path=pod["network_path"],
        shared_dir=pod["shared"], dry_run=False,
        sender=lambda **kw: _Cp(0),
    )
    assert result.success is False
    assert any("not in network.json" in e for e in result.errors)
    assert any("does not exist" in e for e in result.errors)
    # No archive created
    assert not (pod["shared"] / "archived-bots").exists()


def test_delete_bot_completes_cleanup_for_retired_bot(
    tmp_path, patch_bot_paths, monkeypatch,
):
    """If the bot is NOT in network.json but `/Users/<bot_id>/` still
    exists on disk, delete_bot must skip the retire step and complete
    the macOS user cleanup (dscl + rm).

    Surfaced 2026-06-01: an operator ran retire-bot (succeeded — bot
    removed from network), then ran delete-bot expecting it to finish
    the job. The old delete-bot refused with "not in network.json"
    and stranded /Users/<bot>/ on disk. The new code recognizes the
    retired-but-not-deleted state as "complete the cleanup", not
    "refuse"."""
    pod = _make_pod(tmp_path, bot_id="ghost", user="ghost")
    patch_bot_paths(pod["home"])

    # Pin the disk-check to True — the bot home exists, simulating
    # the post-retire state. We mock the dscl subprocess so the test
    # doesn't try to delete a real user account.
    monkeypatch.setattr(
        retire, "_bot_home_exists_on_disk", lambda bot_id: True,
    )
    fake = _FakeSubprocess()
    shim, recorded = _delete_bot_subprocess_recorder(fake)
    monkeypatch.setattr(retire.subprocess, "run", shim)
    from evolve_admin import provisioning
    monkeypatch.setattr(provisioning.subprocess, "run", shim)

    # Patch retire_bot — if delete_bot incorrectly tries to retire a
    # bot that's not in network, this would be called and the test
    # would log a clear "should not have called retire" failure.
    def _should_not_run_retire(*args, **kwargs):
        raise AssertionError(
            "delete_bot called retire_bot on a bot that's no longer "
            "in network.json — the retire step must be skipped when "
            "the bot is already retired."
        )
    monkeypatch.setattr(retire, "retire_bot", _should_not_run_retire)

    # Use a bot id that is NOT in network.json (pod fixture has
    # 'ghost' as a member; pick 'phantom' which is absent).
    result = retire.delete_bot(
        "phantom", network_path=pod["network_path"],
        shared_dir=pod["shared"], dry_run=False,
    )

    assert result.success, result.errors
    # dscl + rm both fired against /Users/phantom
    assert len(recorded["dscl"]) == 1, recorded["dscl"]
    assert recorded["dscl"][0][4] == "/Users/phantom"
    assert len(recorded["rm_home"]) == 1, recorded["rm_home"]
    assert recorded["rm_home"][0][3] == "/Users/phantom"
    # The result log explains what happened so an operator scanning
    # the steps sees that retire was deliberately skipped, not
    # silently bypassed.
    assert any("already retired" in s.lower() for s in result.steps)
    assert any("cleanup only" in s.lower() for s in result.steps)


def test_delete_bot_dry_run_skips_dscl_for_retired_bot(
    tmp_path, patch_bot_paths, monkeypatch,
):
    """Dry-run must NOT call dscl even when the bot is in the
    retired-but-still-on-disk state. Mirrors the existing dry-run
    safety for the in-network code path."""
    pod = _make_pod(tmp_path, bot_id="ghost", user="ghost")
    patch_bot_paths(pod["home"])

    monkeypatch.setattr(
        retire, "_bot_home_exists_on_disk", lambda bot_id: True,
    )
    fake = _FakeSubprocess()
    shim, recorded = _delete_bot_subprocess_recorder(fake)
    monkeypatch.setattr(retire.subprocess, "run", shim)
    from evolve_admin import provisioning
    monkeypatch.setattr(provisioning.subprocess, "run", shim)

    result = retire.delete_bot(
        "phantom", network_path=pod["network_path"],
        shared_dir=pod["shared"], dry_run=True,
    )

    assert result.success, result.errors
    assert recorded["dscl"] == []
    assert recorded["rm_home"] == []
    assert any("[dry-run] would delete" in s for s in result.steps)


def test_delete_bot_skips_user_delete_if_retire_fails(
    tmp_path, patch_bot_paths, monkeypatch,
):
    """If retire fails (e.g. archive step explodes), delete_bot must
    NOT proceed to user deletion. Leaving a half-archived bot is bad;
    nuking the user account on top is worse."""
    pod = _make_pod(tmp_path)
    patch_bot_paths(pod["home"])

    fake = _FakeSubprocess()
    shim, recorded = _delete_bot_subprocess_recorder(fake)
    monkeypatch.setattr(retire.subprocess, "run", shim)
    from evolve_admin import provisioning
    monkeypatch.setattr(provisioning.subprocess, "run", shim)

    # Force the archive step to fail by pointing shared_dir at a path
    # the retire flow can't write to. The retire flow surfaces an error;
    # delete_bot inspects result.success and bails before dscl.
    def _exploding_retire_bot(*args, **kwargs):
        r = RetireResult(bot_id=pod["bot_id"], dry_run=False)
        r.error("simulated archive failure")
        return r
    monkeypatch.setattr(retire, "retire_bot", _exploding_retire_bot)

    result = retire.delete_bot(
        pod["bot_id"], network_path=pod["network_path"],
        shared_dir=pod["shared"], dry_run=False,
    )

    assert result.success is False
    assert any("simulated archive failure" in e for e in result.errors)
    assert recorded["dscl"] == [], (
        "delete_bot must not call dscl when retire fails"
    )
    assert recorded["rm_home"] == []
    assert any("leaving macOS user in place" in s for s in result.steps)
