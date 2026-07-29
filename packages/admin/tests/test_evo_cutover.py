"""Tests for setup_wizard._perform_evo_cutover (Phase E.2.b).

Spec: docs/spec-evo-account-separation-2026-05-25.md §"Phase E.2.b".

The cutover flips evo's gateway plist from UserName=evolve to
UserName=evo, migrates per-bot state from /Users/evolve/.openclaw/
to /Users/evo/.openclaw/, updates network.json, and bootstraps the
rewritten plist. These tests mock every shell-out so they run
anywhere without touching real macOS state.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import setup_wizard
from evolve_admin.runtime import LaunchdScheduler, set_scheduler


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_scheduler():
    """Never leak an injected scheduler into other tests."""
    yield
    set_scheduler(None)


def _fake_scheduler(runner) -> LaunchdScheduler:
    """Seam-injected scheduler — ``runner`` sees every launchctl argv and no
    subprocess is ever spawned (kickstart/bootout are live-traffic
    destructive; tests must never reach the real launchctl)."""
    return LaunchdScheduler(runner=runner)


class _Result:
    """Minimal stand-in for subprocess.run's CompletedProcess."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _write_network(tmp_path: Path, primary_id: str = "evo",
                   current_user: str = "evolve") -> Path:
    """Write a minimal network.json with one primary bot."""
    net = {
        "networkId": "test-pod",
        "primary": primary_id,
        "bots": {
            primary_id: {
                "role": "primary",
                "port": 19030,
                "user": current_user,
            },
        },
        "sharedDir": str(tmp_path / "shared"),
    }
    (tmp_path / "shared").mkdir(parents=True, exist_ok=True)
    np = tmp_path / "network.json"
    np.write_text(json.dumps(net))
    return np


_PLIST_BEFORE = """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>ai.openclaw.evolve-gateway</string>
    <key>UserName</key>
    <string>evolve</string>
    <key>StandardOutPath</key>
    <string>/Users/evolve/.openclaw/logs/gateway.log</string>
    <key>EnvironmentVariables</key>
    <dict>
      <key>HOME</key>
      <string>/Users/evolve</string>
    </dict>
  </dict>
</plist>"""


def _make_oc_json(workspace: str = "/Users/evolve/.openclaw/workspace") -> str:
    return json.dumps({
        "agents": {"defaults": {"workspace": workspace}},
        "gateway": {"port": 19030},
    })


# ── Preconditions ────────────────────────────────────────────────────────────


def test_preconditions_fail_when_evo_user_missing(tmp_path):
    """pwd.getpwnam('evo') raises KeyError → precondition fails."""
    np = _write_network(tmp_path)
    with patch("pwd.getpwnam", side_effect=KeyError("evo")):
        ok, err, _ = setup_wizard._evo_cutover_preconditions(np)
    assert ok is False
    assert "evo" in err
    assert "provision-evo-account" in err


def test_preconditions_fail_when_evo_openclaw_dir_missing(monkeypatch, tmp_path):
    """evo user exists but /Users/evo/.openclaw doesn't → precondition fails."""
    np = _write_network(tmp_path)
    with patch("pwd.getpwnam"):  # evo user exists
        with patch("pathlib.Path.exists", return_value=False):
            ok, err, _ = setup_wizard._evo_cutover_preconditions(np)
    assert ok is False
    assert ".openclaw" in err


def test_preconditions_idempotent_after_cutover(monkeypatch, tmp_path):
    """network.json says user=evo AND /Users/evo/.openclaw/openclaw.json
    exists → ``already_done`` is True; no error.
    """
    np = _write_network(tmp_path, current_user="evo")
    real_exists = Path.exists

    def fake_exists(self):
        s = str(self)
        if s in (
            "/Users/evo/.openclaw",
            "/Users/evo/.openclaw/openclaw.json",
            "/Users/evolve/.openclaw/openclaw.json",
            f"/Library/LaunchDaemons/{setup_wizard.per_bot_gateway_plist_label('evo')}.plist",
        ):
            return True
        return real_exists(self)

    with patch("pwd.getpwnam"):
        with patch("pathlib.Path.exists", new=fake_exists):
            ok, err, ctx = setup_wizard._evo_cutover_preconditions(np)
    assert ok is True
    assert err is None
    assert ctx["already_done"] is True
    assert ctx["primary_id"] == "evo"
    assert ctx["current_user"] == "evo"


def test_preconditions_pass_pre_cutover(monkeypatch, tmp_path):
    """Pre-cutover state: evo exists, /Users/evo/.openclaw exists, but
    openclaw.json doesn't (E.2.a left it empty), and the source side
    has its openclaw.json. → ok=True, already_done=False.
    """
    np = _write_network(tmp_path, current_user="evolve")
    real_exists = Path.exists

    def fake_exists(self):
        s = str(self)
        if s == "/Users/evo/.openclaw":
            return True
        if s == "/Users/evo/.openclaw/openclaw.json":
            return False  # not yet migrated
        if s == "/Users/evolve/.openclaw/openclaw.json":
            return True
        if s == f"/Library/LaunchDaemons/{setup_wizard.per_bot_gateway_plist_label('evo')}.plist":
            return True
        return real_exists(self)

    with patch("pwd.getpwnam"):
        with patch("pathlib.Path.exists", new=fake_exists):
            ok, err, ctx = setup_wizard._evo_cutover_preconditions(np)
    assert ok is True
    assert err is None
    assert ctx["already_done"] is False
    assert ctx["current_user"] == "evolve"


def test_preconditions_fail_when_plist_missing(monkeypatch, tmp_path):
    np = _write_network(tmp_path, current_user="evolve")
    real_exists = Path.exists

    def fake_exists(self):
        s = str(self)
        if s == "/Users/evo/.openclaw":
            return True
        if s == "/Users/evo/.openclaw/openclaw.json":
            return False
        if s == "/Users/evolve/.openclaw/openclaw.json":
            return True
        if s == f"/Library/LaunchDaemons/{setup_wizard.per_bot_gateway_plist_label('evo')}.plist":
            return False
        return real_exists(self)

    with patch("pwd.getpwnam"):
        with patch("pathlib.Path.exists", new=fake_exists):
            ok, err, _ = setup_wizard._evo_cutover_preconditions(np)
    assert ok is False
    assert "gateway plist" in err


def test_preconditions_fail_when_network_has_no_primary(tmp_path):
    np = tmp_path / "network.json"
    np.write_text(json.dumps({"networkId": "test", "bots": {}}))
    with patch("pwd.getpwnam"):
        with patch("pathlib.Path.exists", return_value=True):
            ok, err, _ = setup_wizard._evo_cutover_preconditions(np)
    assert ok is False
    assert "primary" in err


# ── Dry-run mode ─────────────────────────────────────────────────────────────


def test_dry_run_prints_plan_and_returns_true(monkeypatch, tmp_path, capsys):
    np = _write_network(tmp_path, current_user="evolve")
    real_exists = Path.exists

    def fake_exists(self):
        s = str(self)
        if s == "/Users/evo/.openclaw":
            return True
        if s == "/Users/evo/.openclaw/openclaw.json":
            return False
        if s == "/Users/evolve/.openclaw/openclaw.json":
            return True
        if s == f"/Library/LaunchDaemons/{setup_wizard.per_bot_gateway_plist_label('evo')}.plist":
            return True
        return real_exists(self)

    # If dry_run were to leak through, subprocess (or the scheduler seam)
    # would raise — that's how we verify it doesn't.
    def boom(*a, **k):
        raise AssertionError("dry-run must not call subprocess.run")

    def boom_runner(argv):
        raise AssertionError("dry-run must not issue launchctl commands")

    set_scheduler(_fake_scheduler(boom_runner))
    with patch("pwd.getpwnam"), \
         patch("pathlib.Path.exists", new=fake_exists), \
         patch("evolve_admin.setup_wizard.subprocess.run", side_effect=boom):
        ok = setup_wizard._perform_evo_cutover(np, dry_run=True)
    assert ok is True
    out = capsys.readouterr().out
    assert "dry-run" in out.lower()
    assert "Target macOS user" in out
    assert "evo" in out


def test_dry_run_short_circuits_when_already_done(tmp_path, capsys):
    np = _write_network(tmp_path, current_user="evo")
    real_exists = Path.exists

    def fake_exists(self):
        s = str(self)
        if s in (
            "/Users/evo/.openclaw",
            "/Users/evo/.openclaw/openclaw.json",
            "/Users/evolve/.openclaw/openclaw.json",
            f"/Library/LaunchDaemons/{setup_wizard.per_bot_gateway_plist_label('evo')}.plist",
        ):
            return True
        return real_exists(self)

    with patch("pwd.getpwnam"), patch("pathlib.Path.exists", new=fake_exists):
        ok = setup_wizard._perform_evo_cutover(np, dry_run=True)
    assert ok is True
    assert "Already cut over" in capsys.readouterr().out


# ── Step-level helpers ───────────────────────────────────────────────────────


def test_bootout_skips_when_label_not_loaded():
    """If `launchctl list` returns nonzero, the label isn't loaded;
    skip bootout entirely.
    """
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        # launchctl list returns nonzero when label isn't loaded
        return (1, "", "")

    set_scheduler(_fake_scheduler(runner))
    ok = setup_wizard._evo_cutover_bootout("ai.openclaw.evolve-gateway")
    assert ok is True
    # We probed the list, but never called bootout
    assert any(c[1:3] == ["/bin/launchctl", "list"] for c in calls)
    assert not any("bootout" in c for c in calls)


def test_bootout_waits_for_unload(monkeypatch):
    """When label is loaded, bootout is called and we poll until it
    disappears.
    """
    states = iter([
        # initial list — loaded
        0,
        # poll 1 — still loaded
        0,
        # poll 2 — gone
        1,
    ])
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        if argv[1] == "/bin/launchctl" and argv[2] == "list":
            return (next(states), "", "")
        return (0, "", "")

    set_scheduler(_fake_scheduler(runner))
    monkeypatch.setattr(
        "evolve_admin.setup_wizard.time.sleep", lambda s: None,
    )
    ok = setup_wizard._evo_cutover_bootout("ai.openclaw.evolve-gateway")
    assert ok is True
    assert any("bootout" in c for c in calls)


def test_copy_state_copies_each_listed_child(monkeypatch):
    """The deny-list approach enumerates source dir via sudo /bin/ls
    and copies every non-excluded child. Listing returns the
    representative production set; assertion checks excluded entries
    are skipped and everything else is copied.
    """
    calls: list[list[str]] = []
    children = [
        # Representative subset of the production /Users/evolve/.openclaw/
        # children (per 2026-05-25 mini inspection).
        "openclaw.json",
        "agents",
        "workspace",
        "credentials",
        "memory",
        "identity",
        "telegram",
        "plugins",
        "logs",                              # excluded
        "openclaw.json.bak",                 # excluded
        "openclaw.json.last-good",           # excluded
        "openclaw.json.clobbered.2026-04-12T01-09-32-869Z",  # excluded
    ]

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:3] == ["sudo", "/bin/ls", "-1A"]:
            return _Result(returncode=0, stdout="\n".join(children) + "\n")
        return _Result(returncode=0)

    monkeypatch.setattr("evolve_admin.setup_wizard.subprocess.run", fake_run)
    ok, err = setup_wizard._evo_cutover_copy_state("evolve")
    assert ok is True, err
    assert err is None

    cp_calls = [c for c in calls if c[:3] == ["sudo", "/bin/cp", "-a"]]
    cp_names = [Path(c[3]).name for c in cp_calls]

    # Excluded entries skipped
    assert "logs" not in cp_names
    assert "openclaw.json.bak" not in cp_names
    assert "openclaw.json.last-good" not in cp_names
    assert not any(n.startswith("openclaw.json.clobbered.") for n in cp_names)

    # Everything else copied — and bot state we particularly care about
    for required in ("openclaw.json", "agents", "workspace", "credentials",
                     "memory", "identity", "telegram", "plugins"):
        assert required in cp_names, f"{required} not copied; got {cp_names}"

    # Sources under /Users/evolve/, destinations under /Users/evo/
    for c in cp_calls:
        assert c[3].startswith("/Users/evolve/.openclaw/")
        assert c[4].startswith("/Users/evo/.openclaw/")


def test_copy_state_errors_on_empty_source(monkeypatch):
    """A source dir with no children is suspicious — bail rather than
    silently produce a no-op."""
    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["sudo", "/bin/ls", "-1A"]:
            return _Result(returncode=0, stdout="")
        return _Result(returncode=0)

    monkeypatch.setattr("evolve_admin.setup_wizard.subprocess.run", fake_run)
    ok, err = setup_wizard._evo_cutover_copy_state("evolve")
    assert ok is False
    assert "empty" in err.lower() or "nothing to migrate" in err.lower()


def test_copy_state_returns_error_on_cp_failure(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["sudo", "/bin/ls", "-1A"]:
            return _Result(returncode=0, stdout="openclaw.json\nagents\n")
        if cmd[:3] == ["sudo", "/bin/cp", "-a"]:
            return _Result(returncode=1, stderr="permission denied")
        return _Result(returncode=0)

    monkeypatch.setattr("evolve_admin.setup_wizard.subprocess.run", fake_run)
    ok, err = setup_wizard._evo_cutover_copy_state("evolve")
    assert ok is False
    assert "permission denied" in err


def test_should_copy_helper_classification():
    """Direct unit test of the deny-list classifier."""
    assert setup_wizard._evo_cutover_should_copy("openclaw.json") is True
    assert setup_wizard._evo_cutover_should_copy("agents") is True
    assert setup_wizard._evo_cutover_should_copy("memory") is True
    assert setup_wizard._evo_cutover_should_copy("logs") is False
    assert setup_wizard._evo_cutover_should_copy("openclaw.json.bak") is False
    assert setup_wizard._evo_cutover_should_copy(
        "openclaw.json.clobbered.2026-04-12T01-09-32-869Z"
    ) is False
    assert setup_wizard._evo_cutover_should_copy("openclaw.json.last-good") is False


def test_patch_openclaw_json_rewrites_workspace(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["sudo", "/bin/cat"]:
            return _Result(returncode=0, stdout=_make_oc_json())
        if cmd[:3] == ["sudo", "/bin/cp", "/tmp"]:
            # The cp target is the second-to-last arg
            pass
        if cmd[:2] == ["sudo", "/bin/cp"]:
            # Capture the staged temp file
            tmp = cmd[2]
            captured["staged_content"] = Path(tmp).read_text()
            return _Result(returncode=0)
        return _Result(returncode=0)

    monkeypatch.setattr("evolve_admin.setup_wizard.subprocess.run", fake_run)
    ok, err = setup_wizard._evo_cutover_patch_openclaw_json("evolve")
    assert ok is True
    assert err is None
    patched = json.loads(captured["staged_content"])
    assert patched["agents"]["defaults"]["workspace"] == (
        "/Users/evo/.openclaw/workspace"
    )


def test_patch_openclaw_json_idempotent(monkeypatch):
    """If workspace already points at /Users/evo/, leave it alone."""

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["sudo", "/bin/cat"]:
            return _Result(
                returncode=0,
                stdout=_make_oc_json("/Users/evo/.openclaw/workspace"),
            )
        if cmd[:2] == ["sudo", "/bin/cp"]:
            captured["staged_content"] = Path(cmd[2]).read_text()
            return _Result(returncode=0)
        return _Result(returncode=0)

    monkeypatch.setattr("evolve_admin.setup_wizard.subprocess.run", fake_run)
    ok, err = setup_wizard._evo_cutover_patch_openclaw_json("evolve")
    assert ok is True
    patched = json.loads(captured["staged_content"])
    # Still /Users/evo/ (no double-rewrite into /Users/evo/.openclaw/ → /Users/evo/.openclaw/)
    assert patched["agents"]["defaults"]["workspace"] == (
        "/Users/evo/.openclaw/workspace"
    )


def test_update_network_flips_user_field(tmp_path):
    np = _write_network(tmp_path, primary_id="evo", current_user="evolve")
    ok, err = setup_wizard._evo_cutover_update_network(np, "evo")
    assert ok is True
    assert err is None
    written = json.loads(np.read_text())
    assert written["bots"]["evo"]["user"] == "evo"
    # Other fields preserved
    assert written["bots"]["evo"]["port"] == 19030
    assert written["primary"] == "evo"


def test_rewrite_plist_swaps_username_and_paths(monkeypatch, tmp_path):
    plist = tmp_path / "ai.openclaw.evolve-gateway.plist"
    plist.write_text(_PLIST_BEFORE)
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["sudo", "/bin/cat"]:
            return _Result(returncode=0, stdout=plist.read_text())
        if cmd[:2] == ["sudo", "/bin/cp"]:
            # Persist the staged content to the dst so the test can read it
            tmp = cmd[2]
            dst = cmd[3]
            Path(dst).write_text(Path(tmp).read_text())
            captured["written_to"] = dst
            return _Result(returncode=0)
        return _Result(returncode=0)

    monkeypatch.setattr("evolve_admin.setup_wizard.subprocess.run", fake_run)
    ok, err = setup_wizard._evo_cutover_rewrite_plist(plist)
    assert ok is True, err
    new_content = plist.read_text()
    # UserName flipped
    assert "<key>UserName</key>\n    <string>evo</string>" in new_content
    assert "<string>evolve</string>" not in new_content.replace(
        "ai.openclaw.evolve-gateway", "",
    )
    # Label preserved (stays evolve-gateway)
    assert "ai.openclaw.evolve-gateway" in new_content
    # Paths flipped
    assert "/Users/evolve/" not in new_content
    assert "/Users/evolve<" not in new_content  # HOME tag specifically
    assert "/Users/evo/.openclaw/logs/gateway.log" in new_content
    assert "<string>/Users/evo</string>" in new_content


def test_rewrite_plist_errors_when_username_not_found(monkeypatch, tmp_path):
    plist = tmp_path / "x.plist"
    plist.write_text("<plist><dict></dict></plist>")  # no UserName tag

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["sudo", "/bin/cat"]:
            return _Result(returncode=0, stdout=plist.read_text())
        return _Result(returncode=0)

    monkeypatch.setattr("evolve_admin.setup_wizard.subprocess.run", fake_run)
    ok, err = setup_wizard._evo_cutover_rewrite_plist(plist)
    assert ok is False
    assert "UserName" in err


def test_bootstrap_falls_back_to_kickstart():
    """bootstrap fails → try kickstart → succeed."""
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        if "bootstrap" in argv:
            return (1, "", "boot fail")
        if "kickstart" in argv:
            return (0, "", "")
        return (0, "", "")

    set_scheduler(_fake_scheduler(runner))
    ok, err = setup_wizard._evo_cutover_bootstrap(Path("/x.plist"))
    assert ok is True
    assert err is None
    assert any("bootstrap" in c for c in calls)
    assert any("kickstart" in c for c in calls)


def test_bootstrap_reports_both_failures():
    def runner(argv):
        if "bootstrap" in argv:
            return (1, "", "boot fail")
        if "kickstart" in argv:
            return (1, "", "kick fail")
        return (0, "", "")

    set_scheduler(_fake_scheduler(runner))
    ok, err = setup_wizard._evo_cutover_bootstrap(Path("/x.plist"))
    assert ok is False
    assert "boot fail" in err
    assert "kick fail" in err


# ── End-to-end apply mode ────────────────────────────────────────────────────


def test_apply_full_happy_path(monkeypatch, tmp_path, capsys):
    """Full cutover with all steps succeeding."""
    np = _write_network(tmp_path, current_user="evolve")
    plist = tmp_path / "ai.openclaw.evolve-gateway.plist"
    plist.write_text(_PLIST_BEFORE)

    # Path.exists answers for the precondition layer
    real_exists = Path.exists

    def fake_exists(self):
        s = str(self)
        if s == "/Users/evo/.openclaw":
            return True
        if s == "/Users/evo/.openclaw/openclaw.json":
            return False
        if s == "/Users/evolve/.openclaw/openclaw.json":
            return True
        if s == f"/Library/LaunchDaemons/{setup_wizard.per_bot_gateway_plist_label('evo')}.plist":
            return True
        return real_exists(self)

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        # launchctl list — say loaded at first to exercise bootout
        if cmd[1:3] == ["/bin/launchctl", "list"]:
            # First call says loaded (0), poll says gone (1)
            n = sum(1 for c in calls if c[1:3] == ["/bin/launchctl", "list"])
            return _Result(returncode=0 if n == 1 else 1)
        # /bin/ls -1A for the copy-enumeration step — return a small
        # production-shaped child list including one excluded entry
        if cmd[:3] == ["sudo", "/bin/ls", "-1A"]:
            return _Result(returncode=0, stdout=(
                "openclaw.json\nagents\nworkspace\ncredentials\n"
                "memory\nlogs\n"
            ))
        # cat for plist read uses the test fixture plist path; cat for
        # openclaw.json read returns a valid stub
        if cmd[:2] == ["sudo", "/bin/cat"]:
            target = cmd[2]
            if target.endswith(".plist"):
                return _Result(returncode=0, stdout=_PLIST_BEFORE)
            if target.endswith("openclaw.json"):
                return _Result(returncode=0, stdout=_make_oc_json())
            return _Result(returncode=0, stdout="")
        return _Result(returncode=0)

    # set_evolve_read_acl is imported lazily inside the helper; stub it.
    import evolve_admin.deploy
    monkeypatch.setattr(
        evolve_admin.deploy, "set_evolve_read_acl", lambda bot_id: None,
    )

    # The gateway-verify HTTP probe — pretend it's up.
    import urllib.request

    class _DummyResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda url, timeout=3: _DummyResp(),
    )

    # save_network() (reached via the full cutover) fans out to
    # audit_pod_config.sync_all_pods → user_home(bot_user). With pwd.getpwnam
    # mocked, an unpatched user_home stringifies the MagicMock pw_dir and the
    # pod_config write lands at "MagicMock/getpwnam().pw_dir/<id>/..." in the
    # repo root. Redirect home resolution into tmp_path so the write stays
    # sandboxed (honoring this module's "touches no real macOS state" contract).
    with patch("pwd.getpwnam"), \
         patch("pathlib.Path.exists", new=fake_exists), \
         patch("evolve_admin.config.user_home",
               lambda acct: tmp_path / "homes" / str(acct)), \
         patch("evolve_admin.setup_wizard.subprocess.run", new=fake_run), \
         patch("evolve_admin.setup_wizard.time.sleep", lambda s: None):
        ok = setup_wizard._perform_evo_cutover(np, dry_run=False)

    assert ok is True, capsys.readouterr().out

    # network.json now says user=evo
    updated = json.loads(np.read_text())
    assert updated["bots"]["evo"]["user"] == "evo"

    # We hit each of the canonical step shapes at least once
    cmd_strs = [" ".join(c) for c in calls]
    assert any("/bin/launchctl bootout system/" in s for s in cmd_strs)
    assert any("/bin/cp -a /Users/evolve/.openclaw/" in s for s in cmd_strs)
    assert any("/usr/sbin/chown -R evo:staff" in s for s in cmd_strs)
    assert any("/bin/launchctl bootstrap system" in s for s in cmd_strs)


def test_apply_halts_on_bootout_stuck(monkeypatch, tmp_path):
    np = _write_network(tmp_path, current_user="evolve")
    real_exists = Path.exists

    def fake_exists(self):
        s = str(self)
        if s == "/Users/evo/.openclaw":
            return True
        if s == "/Users/evo/.openclaw/openclaw.json":
            return False
        if s == "/Users/evolve/.openclaw/openclaw.json":
            return True
        if s == f"/Library/LaunchDaemons/{setup_wizard.per_bot_gateway_plist_label('evo')}.plist":
            return True
        return real_exists(self)

    def fake_run(cmd, **kwargs):
        # Always say the label is loaded — bootout never lets go.
        if cmd[1:3] == ["/bin/launchctl", "list"]:
            return _Result(returncode=0)
        return _Result(returncode=0)

    with patch("pwd.getpwnam"), \
         patch("pathlib.Path.exists", new=fake_exists), \
         patch("evolve_admin.setup_wizard.subprocess.run", new=fake_run), \
         patch("evolve_admin.setup_wizard.time.sleep", lambda s: None):
        ok = setup_wizard._perform_evo_cutover(np, dry_run=False)

    assert ok is False
    # network.json was NOT changed
    assert json.loads(np.read_text())["bots"]["evo"]["user"] == "evolve"


def test_apply_idempotent_when_already_cut_over(monkeypatch, tmp_path, capsys):
    np = _write_network(tmp_path, current_user="evo")
    real_exists = Path.exists

    def fake_exists(self):
        s = str(self)
        if s in (
            "/Users/evo/.openclaw",
            "/Users/evo/.openclaw/openclaw.json",
            "/Users/evolve/.openclaw/openclaw.json",
            f"/Library/LaunchDaemons/{setup_wizard.per_bot_gateway_plist_label('evo')}.plist",
        ):
            return True
        return real_exists(self)

    def boom(*a, **k):
        raise AssertionError(
            "already-cut-over path must not call subprocess.run"
        )

    with patch("pwd.getpwnam"), \
         patch("pathlib.Path.exists", new=fake_exists), \
         patch("evolve_admin.setup_wizard.subprocess.run", side_effect=boom):
        ok = setup_wizard._perform_evo_cutover(np, dry_run=False)

    assert ok is True
    assert "Already cut over" in capsys.readouterr().out


# ── Backup SSH key migration (Step 5b) ───────────────────────────────────────


class _FakeStatResult:
    """Minimal stand-in for os.stat_result with the fields we read."""

    def __init__(self, mode: int = 0o700, uid: int = 9999):
        # st_mode in real os.stat includes the file-type bits; for a dir
        # that's S_IFDIR (0o40000). We OR them in so stat.S_IMODE strips
        # them back out as it would on a real stat result.
        self.st_mode = 0o040000 | mode
        self.st_uid = uid


class _FakePwd:
    """Stand-in for pwd.getpwnam('evo')."""

    def __init__(self, uid: int = 9999):
        self.pw_uid = uid


def test_migrate_backup_ssh_key_noop_when_source_missing(monkeypatch, tmp_path):
    """Source ssh dir doesn't have any backup key → return ok, no shell calls."""
    monkeypatch.setattr(
        setup_wizard, "EVOLVE_SSH_DIR", tmp_path / "evolve_ssh",
    )
    monkeypatch.setattr(
        setup_wizard, "EVO_SSH_DIR", tmp_path / "evo_ssh",
    )

    def boom(*a, **k):
        raise AssertionError("no-source path must not call subprocess.run")

    monkeypatch.setattr(
        "evolve_admin.setup_wizard.subprocess.run", boom,
    )
    ok, err = setup_wizard._evo_cutover_migrate_backup_ssh_key("evolve")
    assert ok is True
    assert err is None


def test_migrate_backup_ssh_key_copies_when_target_missing(monkeypatch, tmp_path):
    """Source key present, target dir + key absent → mkdir, chmod 700,
    chown, cp privkey + .pub, chown each, chmod 600/644."""
    src = tmp_path / "evolve_ssh"
    dst = tmp_path / "evo_ssh"
    src.mkdir()
    (src / "evolve-backup-evolve").write_text("PRIV")
    (src / "evolve-backup-evolve.pub").write_text("PUB")

    monkeypatch.setattr(setup_wizard, "EVOLVE_SSH_DIR", src)
    monkeypatch.setattr(setup_wizard, "EVO_SSH_DIR", dst)

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        # Simulate sudo mkdir/cp by mirroring filesystem changes so
        # subsequent .exists() checks see the new state.
        if cmd[:2] == ["sudo", "/bin/mkdir"]:
            target = Path(cmd[-1])
            target.mkdir(parents=True, exist_ok=True)
        elif cmd[:2] == ["sudo", "/bin/cp"]:
            Path(cmd[3]).write_text(Path(cmd[2]).read_text())
        return _Result(returncode=0)

    monkeypatch.setattr(
        "evolve_admin.setup_wizard.subprocess.run", fake_run,
    )
    ok, err = setup_wizard._evo_cutover_migrate_backup_ssh_key("evolve")
    assert ok is True, err
    assert err is None

    def has_call(*expected):
        return any(c == list(expected) for c in calls)

    # Directory provisioned
    assert has_call("sudo", "/bin/mkdir", "-p", str(dst))
    assert has_call("sudo", "/bin/chmod", "700", str(dst))
    assert has_call("sudo", "/usr/sbin/chown", "evo:staff", str(dst))
    # Both keys copied with correct ownership + mode
    priv_src = src / "evolve-backup-evolve"
    pub_src = src / "evolve-backup-evolve.pub"
    priv_dst = dst / "evolve-backup-evolve"
    pub_dst = dst / "evolve-backup-evolve.pub"
    assert has_call("sudo", "/bin/cp", str(priv_src), str(priv_dst))
    assert has_call("sudo", "/bin/cp", str(pub_src), str(pub_dst))
    assert has_call("sudo", "/bin/chmod", "600", str(priv_dst))
    assert has_call("sudo", "/bin/chmod", "644", str(pub_dst))
    assert has_call("sudo", "/usr/sbin/chown", "evo:staff", str(priv_dst))
    assert has_call("sudo", "/usr/sbin/chown", "evo:staff", str(pub_dst))


def test_migrate_backup_ssh_key_idempotent_when_target_correct(monkeypatch, tmp_path):
    """Target dir + keys already exist with correct mode/owner → no mkdir,
    no cp; only the idempotent chown/chmod re-assertions on the existing
    files. The dir-level mkdir/chmod/chown are skipped entirely."""
    src = tmp_path / "evolve_ssh"
    dst = tmp_path / "evo_ssh"
    src.mkdir()
    dst.mkdir()
    (src / "evolve-backup-evolve").write_text("PRIV")
    (src / "evolve-backup-evolve.pub").write_text("PUB")
    (dst / "evolve-backup-evolve").write_text("PRIV")
    (dst / "evolve-backup-evolve.pub").write_text("PUB")

    monkeypatch.setattr(setup_wizard, "EVOLVE_SSH_DIR", src)
    monkeypatch.setattr(setup_wizard, "EVO_SSH_DIR", dst)

    # Pretend the dir is already mode 700 owned by uid 9999 (the "evo" uid).
    real_stat = Path.stat

    def fake_stat(self, *a, **k):
        if Path(str(self)) == dst:
            return _FakeStatResult(mode=0o700, uid=9999)
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", fake_stat)
    monkeypatch.setattr("pwd.getpwnam", lambda name: _FakePwd(uid=9999))

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _Result(returncode=0)

    monkeypatch.setattr(
        "evolve_admin.setup_wizard.subprocess.run", fake_run,
    )
    ok, err = setup_wizard._evo_cutover_migrate_backup_ssh_key("evolve")
    assert ok is True, err
    assert err is None

    # Use the raw call list for path-exact assertions (string substring
    # matching is unsafe — the per-file chown target has the dir as a
    # prefix, so "in" would conflate dir-level with file-level commands).
    def has_call(*expected):
        return any(c == list(expected) for c in calls)

    # Dir-level work skipped (state already correct)
    assert not has_call("sudo", "/bin/mkdir", "-p", str(dst))
    assert not has_call("sudo", "/bin/chmod", "700", str(dst))
    assert not has_call("sudo", "/usr/sbin/chown", "evo:staff", str(dst))
    # No re-copy since target files exist
    assert not any(c[:2] == ["sudo", "/bin/cp"] for c in calls)
    # But mode/owner ARE re-asserted on the existing target files (idempotent).
    priv_dst = dst / "evolve-backup-evolve"
    pub_dst = dst / "evolve-backup-evolve.pub"
    assert has_call("sudo", "/bin/chmod", "600", str(priv_dst))
    assert has_call("sudo", "/bin/chmod", "644", str(pub_dst))
    assert has_call("sudo", "/usr/sbin/chown", "evo:staff", str(priv_dst))
    assert has_call("sudo", "/usr/sbin/chown", "evo:staff", str(pub_dst))


def test_migrate_backup_ssh_key_fixes_wrong_dir_mode(monkeypatch, tmp_path):
    """Target dir exists but mode is wrong (0o755) → chmod 700 fires,
    mkdir does not."""
    src = tmp_path / "evolve_ssh"
    dst = tmp_path / "evo_ssh"
    src.mkdir()
    dst.mkdir()
    (src / "evolve-backup-evolve").write_text("PRIV")
    (src / "evolve-backup-evolve.pub").write_text("PUB")

    monkeypatch.setattr(setup_wizard, "EVOLVE_SSH_DIR", src)
    monkeypatch.setattr(setup_wizard, "EVO_SSH_DIR", dst)

    real_stat = Path.stat

    def fake_stat(self, *a, **k):
        if Path(str(self)) == dst:
            return _FakeStatResult(mode=0o755, uid=9999)  # wrong mode
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", fake_stat)
    monkeypatch.setattr("pwd.getpwnam", lambda name: _FakePwd(uid=9999))

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["sudo", "/bin/cp"]:
            Path(cmd[3]).write_text(Path(cmd[2]).read_text())
        return _Result(returncode=0)

    monkeypatch.setattr(
        "evolve_admin.setup_wizard.subprocess.run", fake_run,
    )
    ok, err = setup_wizard._evo_cutover_migrate_backup_ssh_key("evolve")
    assert ok is True, err

    def has_call(*expected):
        return any(c == list(expected) for c in calls)

    assert not any(c[:2] == ["sudo", "/bin/mkdir"] for c in calls)
    assert has_call("sudo", "/bin/chmod", "700", str(dst))
    # Owner already correct (uid 9999) → dir-level chown not needed
    assert not has_call("sudo", "/usr/sbin/chown", "evo:staff", str(dst))


def test_migrate_backup_ssh_key_fixes_wrong_dir_owner(monkeypatch, tmp_path):
    """Target dir exists, right mode but wrong owner (uid != evo's) → chown
    fires, mkdir + chmod do not."""
    src = tmp_path / "evolve_ssh"
    dst = tmp_path / "evo_ssh"
    src.mkdir()
    dst.mkdir()
    (src / "evolve-backup-evolve").write_text("PRIV")
    (src / "evolve-backup-evolve.pub").write_text("PUB")

    monkeypatch.setattr(setup_wizard, "EVOLVE_SSH_DIR", src)
    monkeypatch.setattr(setup_wizard, "EVO_SSH_DIR", dst)

    real_stat = Path.stat

    def fake_stat(self, *a, **k):
        if Path(str(self)) == dst:
            return _FakeStatResult(mode=0o700, uid=501)  # wrong owner
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", fake_stat)
    monkeypatch.setattr("pwd.getpwnam", lambda name: _FakePwd(uid=9999))

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["sudo", "/bin/cp"]:
            Path(cmd[3]).write_text(Path(cmd[2]).read_text())
        return _Result(returncode=0)

    monkeypatch.setattr(
        "evolve_admin.setup_wizard.subprocess.run", fake_run,
    )
    ok, err = setup_wizard._evo_cutover_migrate_backup_ssh_key("evolve")
    assert ok is True, err

    def has_call(*expected):
        return any(c == list(expected) for c in calls)

    assert not any(c[:2] == ["sudo", "/bin/mkdir"] for c in calls)
    assert not has_call("sudo", "/bin/chmod", "700", str(dst))
    assert has_call("sudo", "/usr/sbin/chown", "evo:staff", str(dst))


def test_migrate_backup_ssh_key_also_copies_shared_key(monkeypatch, tmp_path):
    """Both per-bot key AND evolve-backup-shared present → both copied."""
    src = tmp_path / "evolve_ssh"
    dst = tmp_path / "evo_ssh"
    src.mkdir()
    (src / "evolve-backup-evolve").write_text("PRIV1")
    (src / "evolve-backup-evolve.pub").write_text("PUB1")
    (src / "evolve-backup-shared").write_text("PRIV2")
    (src / "evolve-backup-shared.pub").write_text("PUB2")

    monkeypatch.setattr(setup_wizard, "EVOLVE_SSH_DIR", src)
    monkeypatch.setattr(setup_wizard, "EVO_SSH_DIR", dst)

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["sudo", "/bin/mkdir"]:
            Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
        elif cmd[:2] == ["sudo", "/bin/cp"]:
            Path(cmd[3]).write_text(Path(cmd[2]).read_text())
        return _Result(returncode=0)

    monkeypatch.setattr(
        "evolve_admin.setup_wizard.subprocess.run", fake_run,
    )
    ok, err = setup_wizard._evo_cutover_migrate_backup_ssh_key("evolve")
    assert ok is True, err
    # Both per-bot + shared keypairs were copied (privkey + pub each)
    assert (dst / "evolve-backup-evolve").exists()
    assert (dst / "evolve-backup-evolve.pub").exists()
    assert (dst / "evolve-backup-shared").exists()
    assert (dst / "evolve-backup-shared.pub").exists()


def test_migrate_backup_ssh_key_collects_errors_best_effort(monkeypatch, tmp_path):
    """If the cp fails, return ok=False with a diagnostic — but DO NOT raise.
    The caller (_perform_evo_cutover) treats this as non-fatal."""
    src = tmp_path / "evolve_ssh"
    dst = tmp_path / "evo_ssh"
    src.mkdir()
    (src / "evolve-backup-evolve").write_text("PRIV")
    (src / "evolve-backup-evolve.pub").write_text("PUB")

    monkeypatch.setattr(setup_wizard, "EVOLVE_SSH_DIR", src)
    monkeypatch.setattr(setup_wizard, "EVO_SSH_DIR", dst)

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["sudo", "/bin/mkdir"]:
            Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
            return _Result(returncode=0)
        if cmd[:2] == ["sudo", "/bin/cp"]:
            return _Result(returncode=1, stderr="EACCES")
        return _Result(returncode=0)

    monkeypatch.setattr(
        "evolve_admin.setup_wizard.subprocess.run", fake_run,
    )
    ok, err = setup_wizard._evo_cutover_migrate_backup_ssh_key("evolve")
    assert ok is False
    assert "EACCES" in err


# ── describe / dry-run plan lists step 5b ────────────────────────────────────


def test_describe_lists_backup_key_migration_step(tmp_path):
    """The dry-run plan should announce step 5b so the operator knows the
    SSH key migration is part of the cutover."""
    ctx = {
        "primary_id": "evolve",
        "current_user": "evolve",
        "label": "ai.openclaw.evolve-gateway",
        "plist_path": Path("/Library/LaunchDaemons/x.plist"),
        "already_done": False,
    }
    lines = setup_wizard._evo_cutover_describe(ctx)
    joined = "\n".join(lines)
    assert "5b." in joined
    assert "evolve-backup-evolve" in joined or "evolve-backup-{evolve" in joined
    assert "/Users/evo/.ssh/" in joined
