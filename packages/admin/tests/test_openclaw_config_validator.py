"""Tests for evolve_admin.openclaw_config_validator.

The post-pull validator is the Phase 1 defense-in-depth that catches the
PR #1525 class of regression (schema-tightened-existing-config-stale).
These tests pin:

- Per-bot validation succeeds when `openclaw config validate --json`
  reports {"valid": true}.
- Per-bot validation fails cleanly (no Signal but recorded error) when
  the openclaw.json file is missing.
- A failing bot results in a firing Signal with the expected signature,
  producer, scope, and bot_id.
- A bot that was failing and now passes has its Signal auto-resolved
  via sweep_resolve.
- A validator that times out or returns non-JSON is recorded as an
  `error`, not as `valid=False` (Phase 1 intentionally does not emit
  a Signal for the validator-broken class).
- The hook never raises into the caller: import failures of the
  signals package, subprocess errors, etc. all degrade gracefully.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    """Create a tmp shared_dir with the signals subtree initialized."""
    sd = tmp_path / "evolve"
    (sd / "signals" / "firing").mkdir(parents=True)
    (sd / "signals" / "snoozed").mkdir(parents=True)
    (sd / "signals" / "archived").mkdir(parents=True)
    return sd


@pytest.fixture
def network() -> dict:
    return {
        "networkId": "test",
        "members": ["team_bot_a", "team_bot_b"],
        "sharedDir": "/tmp/test",
        "bots": {
            "team_bot_a":  {"port": 18789, "user": "team_bot_a"},
            "team_bot_b": {"port": 18790, "user": "personal_bot_user"},   # bot_id ≠ account name
        },
    }


def _patch_openclaw_files(monkeypatch, bot_to_path: dict[str, Path]):
    """Make Path.exists return True for any path mapped in bot_to_path.values()."""
    real_exists = Path.exists
    valid_paths = set(str(p) for p in bot_to_path.values())

    def fake_exists(self):
        if str(self) in valid_paths:
            return True
        return real_exists(self)
    monkeypatch.setattr(Path, "exists", fake_exists)


# ── validate_bot_openclaw_json ────────────────────────────────────────────


def test_validate_bot_returns_valid_when_oc_says_valid(network, monkeypatch, tmp_path):
    """openclaw config validate exits 0 with {"valid": true} → valid=True."""
    from evolve_admin import openclaw_config_validator as ocv

    # Pretend /Users/team_bot_a/.openclaw/openclaw.json exists.
    fake_path = Path("/Users/team_bot_a/.openclaw/openclaw.json")
    _patch_openclaw_files(monkeypatch, {"team_bot_a": fake_path})

    fake_proc = MagicMock(returncode=0, stdout='{"valid": true, "issues": []}', stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake_proc)

    valid, issues, error = ocv.validate_bot_openclaw_json(
        "team_bot_a", network, openclaw_bin="/fake/openclaw",
    )
    assert valid is True
    assert issues == []
    assert error == ""


def test_validate_bot_returns_invalid_with_issues(network, monkeypatch):
    """openclaw config validate exits 0 with {"valid": false, "issues": [...]} → valid=False."""
    from evolve_admin import openclaw_config_validator as ocv

    fake_path = Path("/Users/team_bot_a/.openclaw/openclaw.json")
    _patch_openclaw_files(monkeypatch, {"team_bot_a": fake_path})

    issues_payload = [
        {
            "path": "plugins.entries.evolve.config",
            "message": "invalid config: must NOT have additional properties",
        }
    ]
    fake_proc = MagicMock(
        returncode=0,
        stdout=json.dumps({"valid": False, "issues": issues_payload}),
        stderr="",
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake_proc)

    valid, issues, error = ocv.validate_bot_openclaw_json(
        "team_bot_a", network, openclaw_bin="/fake/openclaw",
    )
    assert valid is False
    assert error == ""
    assert len(issues) == 1
    assert issues[0]["path"] == "plugins.entries.evolve.config"
    assert "additional properties" in issues[0]["message"]


def test_validate_bot_missing_openclaw_json_records_error(network):
    """If the bot's openclaw.json does not exist, error is set; no Signal class."""
    from evolve_admin import openclaw_config_validator as ocv

    # Don't patch exists — real Path("/Users/team_bot_a/.openclaw/openclaw.json")
    # will return False on a typical dev box.
    valid, issues, error = ocv.validate_bot_openclaw_json("team_bot_a", network)
    assert valid is False
    assert issues == []
    assert "openclaw.json not found" in error


def test_validate_bot_timeout_records_error(network, monkeypatch):
    """A subprocess timeout becomes an `error` (not "invalid")."""
    from evolve_admin import openclaw_config_validator as ocv

    fake_path = Path("/Users/team_bot_a/.openclaw/openclaw.json")
    _patch_openclaw_files(monkeypatch, {"team_bot_a": fake_path})

    def fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="openclaw", timeout=15)
    monkeypatch.setattr(subprocess, "run", fake_run)

    valid, issues, error = ocv.validate_bot_openclaw_json(
        "team_bot_a", network, openclaw_bin="/fake/openclaw",
    )
    assert valid is False
    assert issues == []
    assert "timed out" in error


def test_validate_bot_non_json_output_records_error(network, monkeypatch):
    """Validator output that isn't JSON becomes an error, not "invalid"."""
    from evolve_admin import openclaw_config_validator as ocv

    fake_path = Path("/Users/team_bot_a/.openclaw/openclaw.json")
    _patch_openclaw_files(monkeypatch, {"team_bot_a": fake_path})

    fake_proc = MagicMock(returncode=1, stdout="<html>500</html>", stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake_proc)

    valid, issues, error = ocv.validate_bot_openclaw_json(
        "team_bot_a", network, openclaw_bin="/fake/openclaw",
    )
    assert valid is False
    assert "not JSON" in error


def test_validate_bot_missing_binary_records_error(network, monkeypatch):
    """A FileNotFoundError on subprocess.run becomes an error."""
    from evolve_admin import openclaw_config_validator as ocv

    fake_path = Path("/Users/team_bot_a/.openclaw/openclaw.json")
    _patch_openclaw_files(monkeypatch, {"team_bot_a": fake_path})

    def fake_run(*a, **kw):
        raise FileNotFoundError(2, "No such file")
    monkeypatch.setattr(subprocess, "run", fake_run)

    valid, issues, error = ocv.validate_bot_openclaw_json(
        "team_bot_a", network, openclaw_bin="/fake/openclaw",
    )
    assert valid is False
    assert "binary not found" in error


def test_validate_bot_resolves_user_via_network_for_aliased_bot(network, monkeypatch):
    """team_bot_b lives under /Users/personal_bot_user/ (bot_id ≠ account name)."""
    from evolve_admin import openclaw_config_validator as ocv

    expected_path = Path("/Users/personal_bot_user/.openclaw/openclaw.json")
    _patch_openclaw_files(monkeypatch, {"team_bot_b": expected_path})

    captured: dict = {}
    def fake_run(cmd, **kw):
        captured["env"] = kw.get("env", {})
        return MagicMock(returncode=0, stdout='{"valid": true}', stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    valid, _, _ = ocv.validate_bot_openclaw_json(
        "team_bot_b", network, openclaw_bin="/fake/openclaw",
    )
    assert valid is True
    assert captured["env"]["OPENCLAW_CONFIG_PATH"] == str(expected_path)


def test_validate_bot_launches_openclaw_with_safe_cwd(network, monkeypatch):
    """``openclaw`` MUST be launched with cwd=/tmp (or another evolve-readable
    path). Without this, openclaw dies at startup with EACCES uv_cwd before
    the validator can run — silently turning every bot into "couldn't
    validate" and never firing a Signal. Smoke-tested on the mini 2026-05-25;
    pinning here so a future refactor can't regress."""
    from evolve_admin import openclaw_config_validator as ocv

    _patch_openclaw_files(monkeypatch, {"team_bot_a": Path("/Users/team_bot_a/.openclaw/openclaw.json")})

    captured: dict = {}
    def fake_run(cmd, **kw):
        captured["cwd"] = kw.get("cwd")
        return MagicMock(returncode=0, stdout='{"valid": true}', stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    ocv.validate_bot_openclaw_json("team_bot_a", network, openclaw_bin="/fake/openclaw")
    assert captured["cwd"] == "/tmp"


# ── validate_all_bots: integration with Signal store ──────────────────────


def test_validate_all_bots_emits_signal_for_failing_bot(network, shared_dir, monkeypatch):
    """A bot with an invalid openclaw.json gets a firing Signal."""
    from evolve_admin import openclaw_config_validator as ocv

    _patch_openclaw_files(monkeypatch, {
        "team_bot_a":  Path("/Users/team_bot_a/.openclaw/openclaw.json"),
        "team_bot_b": Path("/Users/personal_bot_user/.openclaw/openclaw.json"),
    })

    def fake_run(cmd, **kw):
        oc_path = kw["env"]["OPENCLAW_CONFIG_PATH"]
        if "team_bot_a" in oc_path:
            return MagicMock(returncode=0, stdout='{"valid": true, "issues": []}', stderr="")
        # team_bot_b is broken
        return MagicMock(
            returncode=0,
            stdout=json.dumps({
                "valid": False,
                "issues": [{
                    "path": "plugins.entries.evolve.config",
                    "message": "must NOT have additional properties",
                }],
            }),
            stderr="",
        )
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(ocv, "_openclaw_bin", lambda: "/fake/openclaw")

    results = ocv.validate_all_bots(network=network, shared_dir=shared_dir)

    assert results["team_bot_a"]["valid"] is True
    assert results["team_bot_b"]["valid"] is False
    assert results["team_bot_b"]["error"] == ""

    firing = list((shared_dir / "signals" / "firing").iterdir())
    assert len(firing) == 1
    sig = json.loads(firing[0].read_text())
    assert sig["producer"] == "openclaw_config_validator"
    assert sig["type"] == "openclaw_invalid"
    assert sig["bot_id"] == "team_bot_b"
    assert sig["scope"] == "bot"
    assert sig["flavor"] == "maintenance"
    assert sig["severity"] == "alert"
    assert "team_bot_b" in sig["title"]
    assert "must NOT have additional properties" in sig["body"]
    assert sig["details"]["issue_count"] == 1


def test_validate_all_bots_does_not_emit_signal_for_validator_error(
    network, shared_dir, monkeypatch
):
    """If the validator couldn't run (binary missing, timeout, etc.), no Signal fires.

    Phase 1 intentionally does not emit a Signal for the "validator
    broken" class — that would be a different finding and conflating
    them on the alerts page hides the real problem.
    """
    from evolve_admin import openclaw_config_validator as ocv

    # No exists patch → all paths return False → all bots record "file missing"
    monkeypatch.setattr(ocv, "_openclaw_bin", lambda: "/fake/openclaw")

    results = ocv.validate_all_bots(network=network, shared_dir=shared_dir)

    assert all(r["error"] for r in results.values())
    firing = list((shared_dir / "signals" / "firing").iterdir())
    assert firing == []


def test_validate_all_bots_sweep_resolves_recovered_bot(network, shared_dir, monkeypatch):
    """A bot that was failing and now passes has its Signal auto-resolved."""
    from evolve_admin import openclaw_config_validator as ocv

    _patch_openclaw_files(monkeypatch, {
        "team_bot_a":  Path("/Users/team_bot_a/.openclaw/openclaw.json"),
        "team_bot_b": Path("/Users/personal_bot_user/.openclaw/openclaw.json"),
    })
    monkeypatch.setattr(ocv, "_openclaw_bin", lambda: "/fake/openclaw")

    # Round 1: team_bot_b is failing → emits a firing Signal.
    def fake_run_round1(cmd, **kw):
        oc_path = kw["env"]["OPENCLAW_CONFIG_PATH"]
        if "team_bot_a" in oc_path:
            return MagicMock(returncode=0, stdout='{"valid": true}', stderr="")
        return MagicMock(
            returncode=0,
            stdout=json.dumps({
                "valid": False,
                "issues": [{"path": "x", "message": "y"}],
            }),
            stderr="",
        )
    monkeypatch.setattr(subprocess, "run", fake_run_round1)
    ocv.validate_all_bots(network=network, shared_dir=shared_dir)
    firing_dir = shared_dir / "signals" / "firing"
    archived_dir = shared_dir / "signals" / "archived"
    assert len(list(firing_dir.iterdir())) == 1

    # Round 2: team_bot_b now passes → sweep_resolve archives the Signal.
    def fake_run_round2(cmd, **kw):
        return MagicMock(returncode=0, stdout='{"valid": true}', stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run_round2)
    ocv.validate_all_bots(network=network, shared_dir=shared_dir)
    assert list(firing_dir.iterdir()) == []
    assert len(list(archived_dir.iterdir())) == 1
    archived = json.loads(list(archived_dir.iterdir())[0].read_text())
    assert archived["state"] == "resolved"
    assert archived["bot_id"] == "team_bot_b"


def test_validate_all_bots_no_network_no_crash(shared_dir):
    """An empty bots map returns empty results — no exceptions, no Signals."""
    from evolve_admin import openclaw_config_validator as ocv

    results = ocv.validate_all_bots(
        network={"networkId": "test", "bots": {}},
        shared_dir=shared_dir,
    )
    assert results == {}


def test_validate_all_bots_continues_when_observe_raises_for_one_bot(
    network, shared_dir, monkeypatch
):
    """If signals.store.observe raises for one bot, the loop continues for
    the next bot AND sweep_resolve still runs at the end. The failing bot
    is not added to kept_signatures, so if a stale firing Signal existed
    for that bot it would be archived — the next tick will re-observe if
    the bot is still invalid."""
    from evolve_admin import openclaw_config_validator as ocv

    _patch_openclaw_files(monkeypatch, {
        "team_bot_a":  Path("/Users/team_bot_a/.openclaw/openclaw.json"),
        "team_bot_b": Path("/Users/personal_bot_user/.openclaw/openclaw.json"),
    })
    monkeypatch.setattr(ocv, "_openclaw_bin", lambda: "/fake/openclaw")

    # Both bots fail validation.
    def fake_run(cmd, **kw):
        return MagicMock(
            returncode=0,
            stdout=json.dumps({
                "valid": False,
                "issues": [{"path": "x", "message": "y"}],
            }),
            stderr="",
        )
    monkeypatch.setattr(subprocess, "run", fake_run)

    # observe() raises for team_bot_a (alphabetically first), then succeeds for team_bot_b.
    real_store, real_schema = ocv._import_signals()
    assert real_store is not None  # sanity: signals must import in the test env

    # Capture the unwrapped reference BEFORE patching — otherwise the flaky
    # wrapper calls itself recursively when we patch back through the
    # attribute access. Same reason MagicMock has unsafe defaults.
    original_observe = real_store.observe
    observe_calls: list[str] = []
    def flaky_observe(shared_dir_, **kw):
        observe_calls.append(kw["bot_id"])
        if kw["bot_id"] == "team_bot_a":
            raise RuntimeError("simulated signal write failure")
        return original_observe(shared_dir_, **kw)

    monkeypatch.setattr(real_store, "observe", flaky_observe)

    results = ocv.validate_all_bots(network=network, shared_dir=shared_dir)

    # Both bots were processed
    assert observe_calls == ["team_bot_a", "team_bot_b"]
    assert results["team_bot_a"]["valid"] is False
    assert results["team_bot_b"]["valid"] is False

    # Only team_bot_b's Signal landed in firing/
    firing = list((shared_dir / "signals" / "firing").iterdir())
    assert len(firing) == 1
    sig = json.loads(firing[0].read_text())
    assert sig["bot_id"] == "team_bot_b"


def test_validate_all_bots_swallows_signal_import_failure(
    network, shared_dir, monkeypatch
):
    """If signals.store can't be imported, validation still runs without crash."""
    from evolve_admin import openclaw_config_validator as ocv

    _patch_openclaw_files(monkeypatch, {"team_bot_a": Path("/Users/team_bot_a/.openclaw/openclaw.json"),
                                         "team_bot_b": Path("/Users/personal_bot_user/.openclaw/openclaw.json")})
    monkeypatch.setattr(ocv, "_openclaw_bin", lambda: "/fake/openclaw")
    monkeypatch.setattr(ocv, "_import_signals", lambda: (None, None))

    def fake_run(cmd, **kw):
        return MagicMock(
            returncode=0,
            stdout=json.dumps({
                "valid": False,
                "issues": [{"path": "x", "message": "y"}],
            }),
            stderr="",
        )
    monkeypatch.setattr(subprocess, "run", fake_run)

    # Should NOT raise. Results carry valid=False for both bots; no
    # firing Signals because the store wasn't reachable.
    results = ocv.validate_all_bots(network=network, shared_dir=shared_dir)
    assert results["team_bot_a"]["valid"] is False
    assert results["team_bot_b"]["valid"] is False
    assert list((shared_dir / "signals" / "firing").iterdir()) == []


# ── repo_puller integration: the post-pull hook ───────────────────────────


def test_pull_hook_records_invalid_bots_on_pullresult(
    network, shared_dir, monkeypatch, tmp_path
):
    """The puller's post-pull hook records invalid_bots on PullResult and
    logs a step. Failing bots show up in `result.openclaw_invalid_bots`."""
    from evolve_admin import repo_puller, openclaw_config_validator as ocv

    # Run an advance-the-HEAD pull so the function reaches the hook block.
    before = "a" * 40
    after = "b" * 40
    git_returns = {
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit one", ""),
        "diff": (0, "packages/analyzer/foo.py", ""),
    }
    def fake_git(repo, args):
        for arg in args:
            if arg.startswith("-"):
                continue
            if arg in git_returns:
                v = git_returns[arg]
                return v.pop(0) if isinstance(v, list) else v
            break
        raise AssertionError(f"unexpected git call: {args}")

    # Stub validate_all_bots so we don't need an actual openclaw binary.
    def fake_validate(network=None, shared_dir=None):
        return {
            "team_bot_a":  {"valid": True,  "issues": [], "error": ""},
            "team_bot_b": {"valid": False, "issues": [{"path": "x", "message": "y"}], "error": ""},
        }
    monkeypatch.setattr(ocv, "validate_all_bots", fake_validate)

    with patch.object(repo_puller, "_git", fake_git), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(repo=tmp_path, shared_dir=shared_dir)

    assert result.success is True
    assert result.openclaw_invalid_bots == ["team_bot_b"]
    assert any(
        "openclaw config validation: 1 bot(s) failing" in s
        for s in result.steps
    )
    # format_for_log surfaces the failing bots prominently
    log_line = repo_puller.format_for_log(result)
    assert "openclaw config invalid for 1 bot(s)" in log_line
    assert "team_bot_b" in log_line


def test_pull_hook_silent_when_all_bots_pass(
    network, shared_dir, monkeypatch, tmp_path
):
    """All-pass case: no invalid_bots, format_for_log doesn't shout."""
    from evolve_admin import repo_puller, openclaw_config_validator as ocv

    before = "a" * 40
    after = "b" * 40
    git_returns = {
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit", ""),
        "diff": (0, "packages/analyzer/foo.py", ""),
    }
    def fake_git(repo, args):
        for arg in args:
            if arg.startswith("-"):
                continue
            if arg in git_returns:
                v = git_returns[arg]
                return v.pop(0) if isinstance(v, list) else v
            break
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(
        ocv, "validate_all_bots",
        lambda network=None, shared_dir=None: {
            "team_bot_a":  {"valid": True, "issues": [], "error": ""},
            "team_bot_b": {"valid": True, "issues": [], "error": ""},
        }
    )

    with patch.object(repo_puller, "_git", fake_git), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(repo=tmp_path, shared_dir=shared_dir)

    assert result.success is True
    assert result.openclaw_invalid_bots == []
    assert any("all bots pass" in s for s in result.steps)
    assert "openclaw config invalid" not in repo_puller.format_for_log(result)


def test_pull_hook_runs_on_noop_pull_too(
    network, shared_dir, monkeypatch, tmp_path
):
    """Validation runs even on no-op (head_before == head_after) pulls.

    Most ticks are no-ops; drift between schema and on-disk config can
    still appear in that window (manual edits, plugin rebuilt last
    tick, etc.). Without this, drift would go unseen until something
    advanced HEAD.
    """
    from evolve_admin import repo_puller, openclaw_config_validator as ocv

    sha = "c" * 40
    git_returns = {
        "rev-parse": [(0, sha, ""), (0, sha, "")],
        "pull": (0, "Already up to date.", ""),
    }
    def fake_git(repo, args):
        for arg in args:
            if arg.startswith("-"):
                continue
            if arg in git_returns:
                v = git_returns[arg]
                return v.pop(0) if isinstance(v, list) else v
            break
        raise AssertionError(f"unexpected git call: {args}")

    calls: list[Path | None] = []
    def fake_validate(network=None, shared_dir=None):
        calls.append(shared_dir)
        return {
            "team_bot_a":  {"valid": True,  "issues": [], "error": ""},
            "team_bot_b": {"valid": False, "issues": [{"path": "x", "message": "y"}], "error": ""},
        }
    monkeypatch.setattr(ocv, "validate_all_bots", fake_validate)

    with patch.object(repo_puller, "_git", fake_git), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(repo=tmp_path, shared_dir=shared_dir)

    assert result.success is True
    assert result.head_before == result.head_after  # confirms it was a no-op
    assert calls == [shared_dir]                    # confirms the hook ran
    assert result.openclaw_invalid_bots == ["team_bot_b"]


def test_pull_hook_swallows_validator_exceptions(
    network, shared_dir, monkeypatch, tmp_path
):
    """If the validator hook raises, the pull still succeeds."""
    from evolve_admin import repo_puller, openclaw_config_validator as ocv

    before = "a" * 40
    after = "b" * 40
    git_returns = {
        "rev-parse": [(0, before, ""), (0, after, "")],
        "pull": (0, "Updating ...", ""),
        "log": (0, "abc commit", ""),
        "diff": (0, "packages/analyzer/foo.py", ""),
    }
    def fake_git(repo, args):
        for arg in args:
            if arg.startswith("-"):
                continue
            if arg in git_returns:
                v = git_returns[arg]
                return v.pop(0) if isinstance(v, list) else v
            break
        raise AssertionError(f"unexpected git call: {args}")

    def boom(**kw):
        raise RuntimeError("synthetic validator explosion")
    monkeypatch.setattr(ocv, "validate_all_bots", boom)

    with patch.object(repo_puller, "_git", fake_git), \
         patch.object(Path, "exists", return_value=True):
        result = repo_puller.pull(repo=tmp_path, shared_dir=shared_dir)

    assert result.success is True
    assert any("validation hook failed" in s for s in result.steps)
