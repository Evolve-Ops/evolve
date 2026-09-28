"""Unit tests for the structured verification block (wave 3a).

Three layers:
  * schema validation — every rejection class has a fixture (the adversarial
    concern is an entry with side effects or an escape from the workspace
    sneaking past review);
  * executor semantics against a fake workspace + injected runner/clock —
    no wall-clock or subprocess coupling;
  * a real-package smoke: task-manager's shipped block validates, and runs
    end-to-end against a synthetic workspace materializing its declared
    files (the repo ships no built files — the forge builds them on-pod).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

_REPO_ROOT = _ADMIN_DIR.parent.parent

from evolve_admin.gallery_verify.model import WARN  # noqa: E402
from evolve_admin.gallery_verify.verification import (  # noqa: E402
    MAX_ENTRIES,
    make_verify_fn,
    run_verification_block,
    validate_entry,
    validate_verification_block,
)

# ── Fixture helpers ───────────────────────────────────────────────────────────


def cmd_entry(**over):
    e = {
        "kind": "command",
        "name": "cli_status",
        "argv": ["python3", "scripts/app.py", "status"],
        "expected_exit": 0,
        "timeout_s": 30,
    }
    e.update(over)
    return e


def art_entry(**over):
    e = {
        "kind": "artifact",
        "name": "config_ok",
        "path": "config.json",
        "checks": ["exists", "json_valid"],
    }
    e.update(over)
    return e


class RecordingRunner:
    """Stands in for subprocess.run; records calls, returns a scripted result."""

    def __init__(self, returncode=0, stdout="", stderr="", raise_exc=None):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def __call__(self, argv, **kw):
        self.calls.append({"argv": argv, **kw})
        if self.raise_exc is not None:
            raise self.raise_exc
        return subprocess.CompletedProcess(
            argv, self.returncode, stdout=self.stdout, stderr=self.stderr,
        )


def run_one(entry, workspace, *, mode="install", runner=None, now=None):
    checks = run_verification_block(
        {"verification": [entry]},
        workspace=str(workspace), mode=mode,
        runner=runner or RecordingRunner(), now=now,
    )
    assert len(checks) == 1
    return checks[0]


# ── Schema validation ─────────────────────────────────────────────────────────


def test_valid_entries_pass():
    assert validate_entry(cmd_entry()) == []
    assert validate_entry(art_entry()) == []
    assert validate_verification_block([cmd_entry(), art_entry()]) == []


def test_block_must_be_list_and_capped():
    assert validate_verification_block({"kind": "command"})
    assert validate_verification_block(
        [cmd_entry(name=f"e{i}") for i in range(MAX_ENTRIES + 1)]
    )


def test_duplicate_names_rejected():
    errs = validate_verification_block([cmd_entry(), cmd_entry()])
    assert any("duplicate" in e for e in errs)


def test_unknown_kind_rejected_by_validation():
    assert any("unknown kind" in e for e in validate_entry({"kind": "run_as_bot", "name": "x"}))


def test_bad_names_rejected():
    for bad in ("", "UPPER", "has space", "x" * 65, None):
        assert validate_entry(cmd_entry(name=bad)), bad


def test_command_interpreter_allowlist():
    # Only bare python3/bash/sh — path-shaped heads and other tools refused.
    for bad in ("python", "node", "/usr/bin/python3", "scripts/app.py", "env"):
        errs = validate_entry(cmd_entry(argv=[bad, "scripts/app.py"]))
        assert errs, bad
    assert validate_entry(cmd_entry(argv=["bash", "scripts/cron.sh"])) == []


def test_command_inline_code_flags_refused():
    # The #2602 boundary: an interpreter's first arg must be a script, never
    # an option flag (-c/-m/-e/stdin all ride through otherwise).
    for argv in (
        ["python3", "-c", "import os"],
        ["python3", "-m", "py_compile", "x.py"],
        ["bash", "-n", "scripts/cron.sh"],
        ["python3"],
    ):
        assert validate_entry(cmd_entry(argv=argv)), argv


def test_command_script_suffix_pinned():
    assert validate_entry(cmd_entry(argv=["python3", "tasks.json"]))
    assert validate_entry(cmd_entry(argv=["bash", "scripts/app.py"]))


def test_absolute_and_traversal_paths_refused():
    for argv in (
        ["python3", "/Users/bot/scripts/app.py"],
        ["python3", "../other-bot/scripts/app.py"],
        ["python3", "scripts/../../etc/app.py"],
    ):
        assert validate_entry(cmd_entry(argv=argv)), argv
    for path in ("/etc/passwd", "../secrets.json", "a/../../b.json"):
        assert validate_entry(art_entry(path=path)), path


def test_shell_evaluation_markers_refused():
    for tok in ("$(date)", "`id`", "${workspace}/x.py", "$HOME"):
        assert validate_entry(cmd_entry(argv=["python3", "scripts/app.py", tok]))


def test_expected_exit_and_timeout_bounds():
    assert validate_entry(cmd_entry(expected_exit=256))
    assert validate_entry(cmd_entry(expected_exit="0"))
    assert validate_entry(cmd_entry(timeout_s=0))
    assert validate_entry(cmd_entry(timeout_s=600))


def test_bad_stdout_regex_rejected():
    assert validate_entry(cmd_entry(stdout_regex="("))
    assert validate_entry(cmd_entry(stdout_regex=""))


def test_artifact_checks_vocabulary():
    assert validate_entry(art_entry(checks=[]))
    assert validate_entry(art_entry(checks=["exists", "exists"]))
    assert validate_entry(art_entry(checks=["writes_file"]))
    assert validate_entry(art_entry(checks=["exists", "owner_exec"])) == []


def test_max_age_bounds():
    assert validate_entry(art_entry(max_age_days=0))
    assert validate_entry(art_entry(max_age_days=-1))
    assert validate_entry(art_entry(max_age_days=400))
    assert validate_entry(art_entry(max_age_days=2)) == []


def test_modes_vocabulary():
    assert validate_entry(cmd_entry(modes=[]))
    assert validate_entry(cmd_entry(modes=["live"]))
    assert validate_entry(cmd_entry(modes=["install", "install"]))
    assert validate_entry(cmd_entry(modes=["dry-run"])) == []


# ── Executor: degenerate block shapes ─────────────────────────────────────────


def test_absent_block_is_skip(tmp_path):
    checks = run_verification_block({}, workspace=str(tmp_path), mode="install")
    assert len(checks) == 1 and checks[0].skipped


def test_empty_block_is_skip_with_meta_note(tmp_path):
    checks = run_verification_block(
        {"verification": []}, workspace=str(tmp_path), mode="install",
    )
    assert checks[0].skipped and "meta package" in checks[0].detail


def test_unresolvable_workspace_is_warn_not_block(tmp_path):
    # Tooling failure (can't find the workspace) must never read as an app
    # failure — WARN, per the distinguish-tooling-failure principle.
    checks = run_verification_block(
        {"verification": [cmd_entry()]}, workspace="", mode="install",
    )
    assert not checks[0].ok and checks[0].severity == WARN


def test_invalid_entry_fails_at_runtime(tmp_path):
    # Imported packages never pass the repo conformance gate — the executor
    # itself must refuse an entry that escapes the schema.
    bad = cmd_entry(argv=["python3", "/Users/victim/.ssh/x.py"])
    check = run_one(bad, tmp_path)
    assert check.failed and "invalid entry" in check.detail


def test_unknown_kind_skips_at_runtime(tmp_path):
    # Forward compat: a future run-as-bot kind on an old harness is absent
    # coverage, not a failure.
    check = run_one({"kind": "run_as_bot", "name": "x"}, tmp_path)
    assert check.skipped and "unknown verification kind" in check.detail


def test_mode_filter_skips(tmp_path):
    check = run_one(cmd_entry(modes=["dry-run"]), tmp_path, mode="install")
    assert check.skipped and "not applicable" in check.detail
    runner = RecordingRunner()
    check = run_one(cmd_entry(modes=["dry-run"]), tmp_path, mode="dry-run", runner=runner)
    assert check.ok and runner.calls


# ── Executor: command entries ─────────────────────────────────────────────────


def test_command_runs_with_workspace_cwd_no_shell(tmp_path):
    runner = RecordingRunner(returncode=0, stdout="ok")
    check = run_one(cmd_entry(), tmp_path, runner=runner)
    assert check.ok
    call = runner.calls[0]
    assert call["argv"] == ["python3", "scripts/app.py", "status"]
    assert call["cwd"] == str(tmp_path)
    assert call["shell"] is False
    assert call["timeout"] == 30


def test_command_nonzero_exit_fails_with_stderr_evidence(tmp_path):
    runner = RecordingRunner(returncode=2, stderr="boom: EACCES writing state")
    check = run_one(cmd_entry(), tmp_path, runner=runner)
    assert check.failed
    assert "exit 2" in check.detail and "EACCES" in check.detail


def test_command_expected_nonzero_exit_passes(tmp_path):
    runner = RecordingRunner(returncode=3)
    check = run_one(cmd_entry(expected_exit=3), tmp_path, runner=runner)
    assert check.ok


def test_command_stdout_regex_miss_fails(tmp_path):
    runner = RecordingRunner(returncode=0, stdout="something else")
    check = run_one(cmd_entry(stdout_regex="(?i)usage"), tmp_path, runner=runner)
    assert check.failed and "did not match" in check.detail


def test_command_stdout_regex_hit_passes(tmp_path):
    runner = RecordingRunner(returncode=0, stdout="Usage: app.py status")
    check = run_one(cmd_entry(stdout_regex="(?i)usage"), tmp_path, runner=runner)
    assert check.ok and "matched" in check.detail


def test_command_timeout_fails(tmp_path):
    runner = RecordingRunner(
        raise_exc=subprocess.TimeoutExpired(cmd="x", timeout=30),
    )
    check = run_one(cmd_entry(), tmp_path, runner=runner)
    assert check.failed and "timed out" in check.detail


def test_command_missing_interpreter_fails(tmp_path):
    runner = RecordingRunner(raise_exc=FileNotFoundError("python3"))
    check = run_one(cmd_entry(), tmp_path, runner=runner)
    assert check.failed and "not found" in check.detail


def test_command_real_subprocess_roundtrip(tmp_path):
    # One real subprocess run (default runner) to prove the seam defaults work.
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "hello.py").write_text("print('hello verify')\n")
    entry = cmd_entry(
        name="real_hello", argv=["python3", "scripts/hello.py"],
        stdout_regex="hello verify",
    )
    check = run_verification_block(
        {"verification": [entry]}, workspace=str(tmp_path), mode="install",
    )[0]
    assert check.ok, check.detail


# ── Executor: artifact entries ────────────────────────────────────────────────


def test_artifact_exists_and_json_valid(tmp_path):
    (tmp_path / "config.json").write_text('{"a": 1}')
    check = run_one(art_entry(), tmp_path)
    assert check.ok and "parses as JSON" in check.detail


def test_artifact_missing_with_exists_fails(tmp_path):
    check = run_one(art_entry(), tmp_path)
    assert check.failed and "no file matched" in check.detail


def test_artifact_missing_without_exists_skips(tmp_path):
    # validate-only-if-present semantics: no "exists" in checks → absent
    # file is absent coverage, not failure.
    check = run_one(art_entry(checks=["json_valid"]), tmp_path)
    assert check.skipped


def test_artifact_invalid_json_fails(tmp_path):
    (tmp_path / "config.json").write_text("{nope")
    check = run_one(art_entry(), tmp_path)
    assert check.failed and "not valid JSON" in check.detail


def test_artifact_py_compiles(tmp_path):
    (tmp_path / "scripts").mkdir()
    good = tmp_path / "scripts" / "good.py"
    good.write_text("x = 1\n")
    bad = tmp_path / "scripts" / "bad.py"
    bad.write_text("def broken(:\n")
    ok = run_one(
        art_entry(name="good", path="scripts/good.py", checks=["exists", "py_compiles"]),
        tmp_path,
    )
    assert ok.ok and "compiles" in ok.detail
    fail = run_one(
        art_entry(name="bad", path="scripts/bad.py", checks=["exists", "py_compiles"]),
        tmp_path,
    )
    assert fail.failed and "does not compile" in fail.detail
    # No __pycache__ minted — the check must be a pure read.
    assert not list(tmp_path.rglob("__pycache__"))


def test_artifact_owner_exec(tmp_path):
    (tmp_path / "scripts").mkdir()
    sh = tmp_path / "scripts" / "cron.sh"
    sh.write_text("#!/bin/bash\n")
    sh.chmod(0o644)
    check = run_one(
        art_entry(name="x", path="scripts/cron.sh", checks=["exists", "owner_exec"]),
        tmp_path,
    )
    assert check.failed and "not owner-executable" in check.detail
    sh.chmod(0o755)
    check = run_one(
        art_entry(name="x", path="scripts/cron.sh", checks=["exists", "owner_exec"]),
        tmp_path,
    )
    assert check.ok


def test_artifact_glob_and_max_age(tmp_path):
    import os
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "2026-07-01.json").write_text("{}")
    entry = art_entry(
        name="runs", path="runs/*.json", checks=["exists", "json_valid"],
        max_age_days=2,
    )
    mtime = (runs / "2026-07-01.json").stat().st_mtime
    fresh = run_one(entry, tmp_path, now=lambda: mtime + 86400)      # 1d old
    assert fresh.ok and "age 1.0d" in fresh.detail
    stale = run_one(entry, tmp_path, now=lambda: mtime + 86400 * 5)  # 5d old
    assert stale.failed and "stale" in stale.detail
    # Newest match wins: add a fresh file, the stale one no longer decides.
    newer = runs / "2026-07-05.json"
    newer.write_text("{}")
    os.utime(newer, (mtime + 86400 * 4.5, mtime + 86400 * 4.5))
    ok_again = run_one(entry, tmp_path, now=lambda: mtime + 86400 * 5)
    assert ok_again.ok, ok_again.detail


def test_artifact_binary_content_fails_block_not_crash(tmp_path):
    # A file declared json_valid/py_compiles but written as non-UTF-8 must
    # FAIL (BLOCK), never raise a UnicodeDecodeError out of the block
    # (finding 1: an escaping ValueError collapsed the block to one WARN).
    (tmp_path / "config.json").write_bytes(b"\xff\xfe\x00\x01 not utf8")
    check = run_one(art_entry(), tmp_path)
    assert check.failed and check.severity != WARN
    assert "not valid UTF-8" in check.detail


def test_artifact_nul_byte_py_compile_fails_block(tmp_path):
    # A NUL byte makes compile() raise a bare ValueError (not SyntaxError).
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "s.py").write_bytes(b"x = 1\x00\n")
    check = run_one(
        art_entry(name="s", path="scripts/s.py", checks=["exists", "py_compiles"]),
        tmp_path,
    )
    assert check.failed and "does not compile" in check.detail


def test_one_raising_entry_does_not_drop_siblings(tmp_path, monkeypatch):
    # The never-raises guarantee: even if an entry executor throws an
    # unexpected exception, that entry FAILs and every sibling still runs.
    import evolve_admin.gallery_verify.verification as V

    real = V._run_artifact
    calls = {"n": 0}

    def boom(entry, workspace, now):
        calls["n"] += 1
        if entry["name"] == "boom":
            raise RuntimeError("unexpected")
        return real(entry, workspace, now)

    monkeypatch.setattr(V, "_run_artifact", boom)
    (tmp_path / "b.json").write_text("{}")
    checks = run_verification_block(
        {"verification": [
            art_entry(name="boom", path="b.json", checks=["exists"]),
            art_entry(name="fine", path="b.json", checks=["exists", "json_valid"]),
        ]},
        workspace=str(tmp_path), mode="install",
    )
    assert len(checks) == 2
    assert checks[0].failed and "entry raised" in checks[0].detail
    assert checks[1].ok  # sibling still ran


def test_nul_and_control_chars_in_tokens_rejected():
    assert validate_entry(cmd_entry(argv=["python3", "scripts/app.py", "a\x00b"]))
    assert validate_entry(cmd_entry(argv=["python3", "scripts/app.py", "a\tb"]))
    assert validate_entry(art_entry(path="conf\x00ig.json"))


# ── Trust gate: imported packages don't execute commands as evolve ────────────


def test_imported_package_skips_commands_runs_artifacts(tmp_path):
    # Finding 2: an imported package's command runs an importer-controlled
    # script AS EVOLVE — gate it. Artifact checks (parse-only) still run.
    (tmp_path / "config.json").write_text("{}")
    runner = RecordingRunner()
    checks = run_verification_block(
        {"source": "imported", "verification": [cmd_entry(), art_entry()]},
        workspace=str(tmp_path), mode="install", runner=runner,
    )
    cmd_check = next(c for c in checks if c.name == "cli_status")
    art_check = next(c for c in checks if c.name == "config_ok")
    assert cmd_check.skipped and "imported" in cmd_check.detail
    assert not runner.calls  # the command was never executed
    assert art_check.ok      # artifact parse still happened


def test_builtin_source_runs_commands(tmp_path):
    runner = RecordingRunner(returncode=0)
    checks = run_verification_block(
        {"source": "builtin", "verification": [cmd_entry()]},
        workspace=str(tmp_path), mode="install", runner=runner,
    )
    assert checks[0].ok and runner.calls


def test_missing_source_treated_as_builtin(tmp_path):
    # Direct calls / unit tests omit source — executor mechanics stay testable.
    runner = RecordingRunner(returncode=0)
    checks = run_verification_block(
        {"verification": [cmd_entry()]},
        workspace=str(tmp_path), mode="install", runner=runner,
    )
    assert checks[0].ok and runner.calls


def test_make_verify_fn_imported_probe_skips_command(tmp_path):
    (tmp_path / "config.json").write_text("{}")
    fn = make_verify_fn(_WsProbe(str(tmp_path)), "install", runner=RecordingRunner())
    checks = fn(
        {"source": "imported", "verification": [cmd_entry(), art_entry()]},
        None, "bot_a",
    )
    assert next(c for c in checks if c.name == "cli_status").skipped
    assert next(c for c in checks if c.name == "config_ok").ok


def test_command_runs_with_new_session(tmp_path):
    runner = RecordingRunner(returncode=0)
    run_one(cmd_entry(), tmp_path, runner=runner)
    assert runner.calls[0].get("start_new_session") is True


def test_artifact_unreadable_is_warn_not_block(tmp_path, monkeypatch):
    # evolve holds a read ACL on bot workspaces; an unreadable file means
    # ACL drift (tooling), not a bad artifact — WARN, never BLOCK.
    (tmp_path / "config.json").write_text("{}")
    monkeypatch.setattr(
        Path, "read_text",
        lambda self, **kw: (_ for _ in ()).throw(PermissionError("denied")),
    )
    check = run_one(art_entry(), tmp_path)
    assert not check.ok and check.severity == WARN and "ACL" in check.detail


# ── make_verify_fn seam ───────────────────────────────────────────────────────


class _WsProbe:
    def __init__(self, ws):
        self._ws = ws

    def bot_workspace(self, bot_id):
        return self._ws


def test_make_verify_fn_resolves_workspace(tmp_path):
    (tmp_path / "config.json").write_text("{}")
    fn = make_verify_fn(_WsProbe(str(tmp_path)), "install")
    checks = fn({"verification": [art_entry()]}, None, "bot_a")
    assert len(checks) == 1 and checks[0].ok


def test_make_verify_fn_probe_error_is_warn():
    class Boom:
        def bot_workspace(self, bot_id):
            raise RuntimeError("no network.json")

    fn = make_verify_fn(Boom(), "install")
    checks = fn({"verification": [art_entry()]}, None, "bot_a")
    assert not checks[0].ok and checks[0].severity == WARN


# ── Real-package smoke: task-manager ──────────────────────────────────────────


def _load_task_manager():
    path = _REPO_ROOT / "gallery" / "task-manager" / "p-9bfa1c84.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_task_manager_block_validates():
    pkg = _load_task_manager()
    assert validate_verification_block(pkg["verification"]) == []


def test_task_manager_block_runs_against_synthetic_workspace(tmp_path):
    """End-to-end over the real shipped block. The repo ships no built files
    (the forge materializes them on-pod), so the workspace is synthesized
    from the block's own declared paths."""
    pkg = _load_task_manager()
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "unified_task_system.py").write_text("x = 1\n")
    for sh in ("task-manager-cache-cron.sh", "task-manager-prune-cron.sh"):
        p = scripts / sh
        p.write_text("#!/bin/bash\n")
        p.chmod(0o755)
    (tmp_path / "tag_registry.json").write_text('{"tags": {}}')
    (tmp_path / "TASKS.md").write_text("# Tasks\n")
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "next-up.md").write_text("")

    runner = RecordingRunner(returncode=0, stdout="usage: unified_task_system")
    now = (tmp_path / "memory" / "next-up.md").stat().st_mtime + 3600

    for mode in ("install", "dry-run"):
        checks = run_verification_block(
            pkg, workspace=str(tmp_path), mode=mode,
            runner=runner, now=lambda: now,
        )
        assert len(checks) == len(pkg["verification"])
        failed = [c for c in checks if c.failed]
        assert not failed, [(c.name, c.detail) for c in failed]
        if mode == "install":
            # the dry-run-only freshness entry must be skipped, not run
            skipped = [c for c in checks if c.skipped]
            assert [c.name for c in skipped] == ["next_up_cache_fresh"]

    # Every command the block ran stayed inside the synthetic workspace and
    # on the interpreter allowlist — the no-side-effects audit in one line.
    for call in runner.calls:
        assert call["cwd"] == str(tmp_path)
        assert call["argv"][0] == "python3"
        assert not any(t.startswith("/") for t in call["argv"])
