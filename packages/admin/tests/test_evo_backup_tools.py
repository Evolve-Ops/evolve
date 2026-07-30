"""tests/test_evo_backup_tools.py — action.bot.backup_workspace +
pod_state.backup_status guards.

These two tools were added in response to a 2026-05-19 transcript
where evo, asked about on-demand git backup, said "Exec is denied in
this session" — the third recurrence in five days of a "fabricated
permission tier" pattern. The structural fix is:

  * ``action.bot.backup_workspace`` — wraps the same launchd job that
    runs ``analyzer/backup.py`` nightly. Triggers on-demand via
    ``sudo /bin/launchctl kickstart -k system/ai.evolve.<bot>.backup``.

  * ``pod_state.backup_status`` — surfaces last-commit timestamp,
    schedule, configured-vs-actual remote, never-run flag, and drift
    flag. Centralizes the answer to "is X backed up?" so the model
    doesn't reach for shell to inspect.

Tests patch path helpers + subprocess so we never actually touch
``/Library/LaunchDaemons`` or spawn ``git`` on real bot workspaces.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))


# ─── Fixtures ────────────────────────────────────────────────────────────────


def _seed_network(
    tmp_path: Path,
    bot_id: str = "team_bot_a",
    backup_repo_url: str | None = "git@github.com:test/team_bot_a-workspace.git",
) -> Path:
    """Write a minimal network.json with one bot. ``backup_repo_url`` of
    None omits backupRepoUrl (configured_remote becomes None)."""
    p = tmp_path / "network.json"
    bot_cfg: dict = {"role": "member"}
    if backup_repo_url is not None:
        bot_cfg["backupRepoUrl"] = backup_repo_url
    p.write_text(json.dumps({
        "networkId": "test-pod",
        "bots": {bot_id: bot_cfg},
    }))
    return p


def _seed_plist(plist_path: Path, hour: int = 2, minute: int = 0) -> None:
    """Write a minimal launchd plist with StartCalendarInterval."""
    import plistlib
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    with open(plist_path, "wb") as f:
        plistlib.dump({
            "Label": "ai.evolve.test.backup",
            "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        }, f)


def _patch_paths(monkeypatch, module, tmp_path: Path, bot_id: str):
    """Redirect the module's path helpers at tmp_path so we don't
    touch /Library/LaunchDaemons or /Users/<bot>/. Returns the
    fake plist + workspace paths so the test can seed them."""
    fake_plist = tmp_path / "LaunchDaemons" / f"ai.evolve.{bot_id}.backup.plist"
    fake_workspace = tmp_path / "Users" / bot_id / ".openclaw" / "workspace"
    monkeypatch.setattr(
        module, "_backup_plist_path", lambda b: fake_plist if b == bot_id else
        tmp_path / "LaunchDaemons" / f"ai.evolve.{b}.backup.plist",
    )
    if hasattr(module, "_workspace_path"):
        monkeypatch.setattr(
            module, "_workspace_path",
            lambda b: fake_workspace if b == bot_id else
            tmp_path / "Users" / b / ".openclaw" / "workspace",
        )
    return fake_plist, fake_workspace


# ─── action.bot.backup_workspace ─────────────────────────────────────────────


def test_backup_workspace_validate_rejects_unknown_bot(tmp_path):
    """Validate must surface a clear reason when bot isn't in network.json
    — better than rendering a button that errors at click time."""
    from evolve_admin.evo.tools import action_bot
    network_path = _seed_network(tmp_path, bot_id="team_bot_a")
    r = action_bot._backup_workspace_validate(
        network_path=network_path, bot_id="ghost",
    )
    assert r["ok"] is False
    assert "ghost" in r["reason"]


def test_backup_workspace_validate_requires_plist(monkeypatch, tmp_path):
    """If the launchd plist isn't installed, the bot isn't configured
    for backup. Validate must say so explicitly so evo can route the
    operator to ``evolve-admin deploy`` instead of clicking a confirm
    that would error."""
    from evolve_admin.evo.tools import action_bot
    network_path = _seed_network(tmp_path, bot_id="team_bot_a")
    _patch_paths(monkeypatch, action_bot, tmp_path, "team_bot_a")
    r = action_bot._backup_workspace_validate(
        network_path=network_path, bot_id="team_bot_a",
    )
    assert r["ok"] is False
    # The reason must mention the deploy step — that's the operator's
    # next action.
    assert "deploy" in r["reason"]


def test_backup_workspace_validate_happy_path(monkeypatch, tmp_path):
    """When bot + plist both exist, validate returns ok=True with
    context (estimated duration, launchd label) the proxy uses to
    render an informative confirm button."""
    from evolve_admin.evo.tools import action_bot
    network_path = _seed_network(tmp_path, bot_id="team_bot_a")
    fake_plist, _ = _patch_paths(monkeypatch, action_bot, tmp_path, "team_bot_a")
    _seed_plist(fake_plist)

    r = action_bot._backup_workspace_validate(
        network_path=network_path, bot_id="team_bot_a",
    )
    assert r["ok"] is True
    assert r["context"]["bot_id"] == "team_bot_a"
    assert r["context"]["launchd_label"] == "ai.evolve.team_bot_a.backup"


def test_backup_workspace_handler_kicks_launchd(monkeypatch, tmp_path):
    """Happy path: handler runs ``sudo /bin/launchctl kickstart -k
    system/ai.evolve.<bot>.backup`` and returns ok=True with a
    verify_via pointing at pod_state.backup_status."""
    from evolve_admin.evo.tools import action_bot
    network_path = _seed_network(tmp_path, bot_id="team_bot_a")
    fake_plist, _ = _patch_paths(monkeypatch, action_bot, tmp_path, "team_bot_a")
    _seed_plist(fake_plist)

    captured: dict = {}

    def _fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    # The handler imports subprocess locally; patch the module-level one
    # since the local import resolves to the same module object.
    monkeypatch.setattr(subprocess, "run", _fake_run)

    r = action_bot._backup_workspace_handler(
        network_path=network_path, bot_id="team_bot_a", reason="test",
    )
    assert r["ok"] is True
    assert r["bot_id"] == "team_bot_a"
    assert r["launchd_label"] == "ai.evolve.team_bot_a.backup"
    assert r["verify_via"]["tool"] == "pod_state.backup_status"
    assert r["verify_via"]["args"] == {"bot_id": "team_bot_a"}

    # Verify the launchctl invocation shape.
    assert captured["cmd"][0] == "sudo"
    assert captured["cmd"][1] == "/bin/launchctl"
    assert captured["cmd"][2:4] == ["kickstart", "-k"]
    assert captured["cmd"][4] == "system/ai.evolve.team_bot_a.backup"


def test_backup_workspace_handler_propagates_launchctl_failure(monkeypatch, tmp_path):
    """If launchctl exits non-zero, the handler must return ok=False
    with the stderr surfaced — not a generic 'failed' string.

    (Post Scheduler-seam migration the exit code itself is no longer
    embedded in the message — ``Scheduler.restart`` reports combined
    output — but the launchctl diagnostic text must still come through.)
    """
    from evolve_admin.evo.tools import action_bot
    network_path = _seed_network(tmp_path, bot_id="team_bot_a")
    fake_plist, _ = _patch_paths(monkeypatch, action_bot, tmp_path, "team_bot_a")
    _seed_plist(fake_plist)

    def _fake_run(cmd, **kw):
        return subprocess.CompletedProcess(
            cmd, 113, stdout="",
            stderr="Could not find service \"ai.evolve.team_bot_a.backup\" in domain for system",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)

    r = action_bot._backup_workspace_handler(
        network_path=network_path, bot_id="team_bot_a", reason="test",
    )
    assert r["ok"] is False
    assert "Could not find service" in r["error"]


def test_backup_workspace_handler_rejects_missing_plist(monkeypatch, tmp_path):
    """Even if validate is bypassed, the handler itself must refuse
    when the plist is missing — defense in depth so a direct tool call
    (no validate path) still does the right thing."""
    from evolve_admin.evo.tools import action_bot
    network_path = _seed_network(tmp_path, bot_id="team_bot_a")
    _patch_paths(monkeypatch, action_bot, tmp_path, "team_bot_a")
    # Note: not seeding the plist file.

    r = action_bot._backup_workspace_handler(
        network_path=network_path, bot_id="team_bot_a",
    )
    assert r["ok"] is False
    assert "plist" in r["error"]


def test_backup_workspace_tool_registered():
    """Tool must appear in the registry under the expected name with
    the expected risk tier. Catches a refactor that drops the
    ``register()`` call."""
    from evolve_admin.evo.tools import RiskTier, lookup
    t = lookup("action.bot.backup_workspace")
    assert t is not None, (
        "action.bot.backup_workspace not registered. Did the import "
        "side-effect in evolve_admin.evo.tools.action_bot get removed?"
    )
    assert t.risk_tier == RiskTier.WRITE_SAFE, (
        "backup_workspace should be WRITE_SAFE — it's reversible (the "
        "backup just adds a commit; nothing destructive) and the blast "
        "radius is one bot's git history."
    )
    assert t.validate is not None, (
        "WRITE_SAFE tools must define validate() so the proxy can "
        "render a clear-reason error instead of a button that errors "
        "at click time."
    )


# ─── pod_state.backup_status ─────────────────────────────────────────────────


def test_backup_status_handler_unconfigured_bot(monkeypatch, tmp_path):
    """Bot in network.json but no plist installed — backup_configured
    must be False and schedule None. Operator's next move is
    ``evolve-admin deploy``."""
    from evolve_admin.evo.tools import pod_state_backup
    network_path = _seed_network(tmp_path, bot_id="team_bot_a", backup_repo_url=None)
    _patch_paths(monkeypatch, pod_state_backup, tmp_path, "team_bot_a")
    # No plist, no workspace seeded.

    r = pod_state_backup._backup_status_handler(
        network_path=network_path, bot_id="team_bot_a",
    )
    assert r["bot_id"] == "team_bot_a"
    assert r["backup_configured"] is False
    assert r["schedule"] is None
    # never_run requires backup_configured AND workspace_exists.
    # Neither is true here, so we should NOT call it "never_run" —
    # the more accurate state is "not configured for backup at all".
    assert r["never_run"] is False


def test_backup_status_handler_returns_schedule_from_plist(monkeypatch, tmp_path):
    """When the plist is installed with Hour+Minute, schedule must be
    populated. The 'human' field is what evo paraphrases to the operator."""
    from evolve_admin.evo.tools import pod_state_backup
    network_path = _seed_network(tmp_path, bot_id="team_bot_a")
    fake_plist, _ = _patch_paths(monkeypatch, pod_state_backup, tmp_path, "team_bot_a")
    _seed_plist(fake_plist, hour=2, minute=0)

    r = pod_state_backup._backup_status_handler(
        network_path=network_path, bot_id="team_bot_a",
    )
    assert r["backup_configured"] is True
    assert r["schedule"] == {"hour": 2, "minute": 0, "human": "daily at 02:00"}


def test_backup_status_handler_never_run(monkeypatch, tmp_path):
    """Plist installed + workspace exists as a git repo + no [backup]
    commit yet → never_run=True. This is the "I deployed but the
    nightly hasn't fired yet" case."""
    from evolve_admin.evo.tools import pod_state_backup
    network_path = _seed_network(tmp_path, bot_id="team_bot_a")
    fake_plist, fake_workspace = _patch_paths(
        monkeypatch, pod_state_backup, tmp_path, "team_bot_a",
    )
    _seed_plist(fake_plist)
    fake_workspace.mkdir(parents=True)

    # Initialize a real git repo and add a non-backup commit so
    # `_last_backup_commit` returns None (no [backup]-prefixed commit yet).
    subprocess.run(["git", "init", "-q", str(fake_workspace)], check=True)
    subprocess.run(
        ["git", "-C", str(fake_workspace), "config", "user.email", "t@t"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(fake_workspace), "config", "user.name", "t"],
        check=True,
    )
    (fake_workspace / "README.md").write_text("x")
    subprocess.run(
        ["git", "-C", str(fake_workspace), "add", "README.md"], check=True,
    )
    subprocess.run(
        ["git", "-C", str(fake_workspace), "commit", "-q", "-m", "non-backup commit"],
        check=True,
    )

    r = pod_state_backup._backup_status_handler(
        network_path=network_path, bot_id="team_bot_a",
    )
    assert r["backup_configured"] is True
    assert r["workspace_exists"] is True
    assert r["last_commit_at"] is None
    assert r["never_run"] is True


def test_backup_status_handler_surfaces_last_backup_commit(monkeypatch, tmp_path):
    """When the workspace has a real ``[backup]``-prefixed commit, the
    handler must return the iso timestamp + short sha + subject line.
    These are what evo paraphrases to the operator ('team_bot_a last backed
    up at <ts>, sha abc1234, "[backup] team_bot_a 2026-05-19T08:00:00Z…"')."""
    from evolve_admin.evo.tools import pod_state_backup
    network_path = _seed_network(tmp_path, bot_id="team_bot_a")
    fake_plist, fake_workspace = _patch_paths(
        monkeypatch, pod_state_backup, tmp_path, "team_bot_a",
    )
    _seed_plist(fake_plist)
    fake_workspace.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(fake_workspace)], check=True)
    subprocess.run(
        ["git", "-C", str(fake_workspace), "config", "user.email", "t@t"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(fake_workspace), "config", "user.name", "t"],
        check=True,
    )
    (fake_workspace / "evolve-backup").mkdir()
    (fake_workspace / "evolve-backup" / "openclaw.json").write_text("{}")
    subprocess.run(
        ["git", "-C", str(fake_workspace), "add", "-A"], check=True,
    )
    subprocess.run(
        ["git", "-C", str(fake_workspace), "commit", "-q",
         "-m", "[backup] team_bot_a 2026-05-19T08:00:00Z"],
        check=True,
    )

    r = pod_state_backup._backup_status_handler(
        network_path=network_path, bot_id="team_bot_a",
    )
    assert r["last_commit_at"] is not None
    assert r["last_commit_subject"].startswith("[backup] team_bot_a ")
    assert len(r["last_commit_sha"]) == 12  # short sha
    assert r["never_run"] is False


def test_backup_status_handler_detects_remote_drift(monkeypatch, tmp_path):
    """When the workspace's live origin URL differs from network.json's
    backupRepoUrl, remote_drift must be True. That's the case where the
    next nightly run will reset the remote (per backup.py::_ensure_remote)
    — the operator may want to know before it happens."""
    from evolve_admin.evo.tools import pod_state_backup
    network_path = _seed_network(
        tmp_path, bot_id="team_bot_a",
        backup_repo_url="git@github.com:configured/team_bot_a.git",
    )
    fake_plist, fake_workspace = _patch_paths(
        monkeypatch, pod_state_backup, tmp_path, "team_bot_a",
    )
    _seed_plist(fake_plist)
    fake_workspace.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(fake_workspace)], check=True)
    subprocess.run(
        ["git", "-C", str(fake_workspace), "remote", "add", "origin",
         "git@github.com:actual/team_bot_a.git"],
        check=True,
    )

    r = pod_state_backup._backup_status_handler(
        network_path=network_path, bot_id="team_bot_a",
    )
    assert r["configured_remote"] == "git@github.com:configured/team_bot_a.git"
    assert r["git_remote"] == "git@github.com:actual/team_bot_a.git"
    assert r["remote_drift"] is True


def test_backup_status_handler_no_drift_when_remotes_match(monkeypatch, tmp_path):
    """When both remotes agree, remote_drift must be False. Catches a
    bug where the comparison logic might fire on the happy path."""
    from evolve_admin.evo.tools import pod_state_backup
    same = "git@github.com:org/team_bot_a.git"
    network_path = _seed_network(tmp_path, bot_id="team_bot_a", backup_repo_url=same)
    fake_plist, fake_workspace = _patch_paths(
        monkeypatch, pod_state_backup, tmp_path, "team_bot_a",
    )
    _seed_plist(fake_plist)
    fake_workspace.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(fake_workspace)], check=True)
    subprocess.run(
        ["git", "-C", str(fake_workspace), "remote", "add", "origin", same],
        check=True,
    )

    r = pod_state_backup._backup_status_handler(
        network_path=network_path, bot_id="team_bot_a",
    )
    assert r["remote_drift"] is False


def test_backup_status_tool_registered():
    """Tool must appear in the registry under the expected name with
    the expected risk tier (read)."""
    from evolve_admin.evo.tools import RiskTier, lookup
    t = lookup("pod_state.backup_status")
    assert t is not None, (
        "pod_state.backup_status not registered. Did the import "
        "side-effect in evolve_admin.evo.tools.pod_state_backup get removed?"
    )
    assert t.risk_tier == RiskTier.READ, (
        "backup_status should be READ — pure inspection, no mutation."
    )
    assert t.validate is None, (
        "Read-tier tools must NOT define validate (would be unreachable; "
        "the Tool dataclass post_init rejects it anyway)."
    )


# ─── Smoke: handlers accept network_path via mcp_server bridge ───────────────


def test_backup_handlers_accept_network_path_via_bridge():
    """The mcp_server bridge inspects handler signatures to decide
    whether to inject ``network_path``. Both new handlers must take
    that kwarg so the bridge wires them correctly."""
    import inspect
    from evolve_admin.evo.tools import action_bot, pod_state_backup
    bw_params = inspect.signature(action_bot._backup_workspace_handler).parameters
    bs_params = inspect.signature(pod_state_backup._backup_status_handler).parameters
    assert "network_path" in bw_params
    assert "network_path" in bs_params
