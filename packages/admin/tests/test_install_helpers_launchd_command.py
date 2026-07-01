"""Tests for ``install_helpers``'s launchd command-mechanism path.

Four slices of coverage:

- ``_build_command_plist_xml`` round-trips through ``plistlib`` and renders
  the expected blocks (ProgramArguments, schedule, env, cwd, UserName).
- ``_substitute_install_vars`` / ``_split_command`` argument handling.
- ``install_launchd_command_action`` with mocked
  ``install_launchd_system_daemon`` — validates the end-to-end shape
  without touching the real filesystem or launchctl.
- ``install_launchd_system_daemon`` itself (one-shot dry-run with
  ``bootstrap=False``) — confirms it writes to /Library/LaunchDaemons/
  and rejects unsafe input.

The 2026-06-04 Atlas Daily Digest incident surfaced that previously NO
unit coverage existed for the launchd-mechanism install path — the only
runtime exercise was on real bots. These tests close that gap.
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path
from unittest import mock

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import install_helpers  # noqa: E402
from evolve_admin.applications.install_helpers import (  # noqa: E402
    _build_command_plist_xml,
    _split_command,
    _substitute_install_vars,
    install_launchd_command_action,
)

# (_xml_escape was retired in the 4.3 C S0 plist consolidation — escaping
# is plistlib's job inside render_launchd_plist; see
# test_plist_xml_escapes_xml_special_chars_in_label below for the
# behavior-level guarantee.)


# ── _substitute_install_vars ─────────────────────────────────────────────────


def test_substitute_expands_known_placeholders() -> None:
    s = "com.${bot_id}.atlas-digest"
    out = _substitute_install_vars(s, bot_id="atlas", workspace="/Users/atlas/.openclaw/workspace")
    assert out == "com.atlas.atlas-digest"


def test_substitute_expands_workspace_placeholder() -> None:
    s = "${workspace}/scripts/foo.sh"
    out = _substitute_install_vars(s, bot_id="atlas", workspace="/ws")
    assert out == "/ws/scripts/foo.sh"


def test_substitute_passes_other_dollar_braces_through() -> None:
    # Other ``${...}`` forms (e.g. real shell vars in commands) survive
    # untouched. We don't try to be a general template engine.
    s = "/bin/bash -c 'echo ${HOME}'"
    out = _substitute_install_vars(s, bot_id="atlas", workspace="/ws")
    assert out == "/bin/bash -c 'echo ${HOME}'"


def test_substitute_tolerates_non_string() -> None:
    assert _substitute_install_vars(None, bot_id="x", workspace="/ws") is None
    assert _substitute_install_vars(42, bot_id="x", workspace="/ws") == 42


# ── _split_command ───────────────────────────────────────────────────────────


def test_split_command_parses_shell_style_quotes() -> None:
    args = _split_command("/bin/bash -c 'python3 scripts/run.py --arg=foo'")
    assert args == ["/bin/bash", "-c", "python3 scripts/run.py --arg=foo"]


def test_split_command_rejects_relative_program() -> None:
    with pytest.raises(ValueError, match="absolute path"):
        _split_command("python3 scripts/foo.py")


def test_split_command_rejects_empty() -> None:
    with pytest.raises(ValueError, match="empty"):
        _split_command("   ")


# ── _build_command_plist_xml — parse it back with plistlib ──────────────────


def _parse(plist_xml: str) -> dict:
    """Round-trip the rendered XML through plistlib so test assertions can
    use Python dicts instead of string-matching XML."""
    return plistlib.loads(plist_xml.encode("utf-8"))


def test_plist_xml_basic_shape_round_trips() -> None:
    xml = _build_command_plist_xml(
        label             = "com.atlas.digest",
        program_arguments = ["/bin/bash", "/Users/atlas/.openclaw/workspace/scripts/cron.sh"],
        schedule          = {"cron": {"Hour": 7, "Minute": 0}},
        log_path          = "/tmp/com.atlas.digest.out.log",
        err_log_path      = "/tmp/com.atlas.digest.err.log",
    )
    d = _parse(xml)
    assert d["Label"] == "com.atlas.digest"
    assert d["ProgramArguments"] == [
        "/bin/bash",
        "/Users/atlas/.openclaw/workspace/scripts/cron.sh",
    ]
    assert d["StartCalendarInterval"] == {"Hour": 7, "Minute": 0}
    assert d["StandardOutPath"] == "/tmp/com.atlas.digest.out.log"
    assert d["StandardErrorPath"] == "/tmp/com.atlas.digest.err.log"
    assert d["RunAtLoad"] is False
    # WorkingDirectory + EnvironmentVariables are omitted when not provided.
    assert "WorkingDirectory" not in d
    assert "EnvironmentVariables" not in d


def test_plist_xml_emits_working_directory_when_set() -> None:
    xml = _build_command_plist_xml(
        label             = "com.atlas.digest",
        program_arguments = ["/bin/bash", "scripts/cron.sh"],
        schedule          = {"cron": {"Hour": 7, "Minute": 0}},
        log_path          = "/tmp/x.out.log",
        err_log_path      = "/tmp/x.err.log",
        cwd               = "/Users/atlas/.openclaw/workspace",
    )
    d = _parse(xml)
    assert d["WorkingDirectory"] == "/Users/atlas/.openclaw/workspace"


def test_plist_xml_emits_environment_variables_when_set() -> None:
    xml = _build_command_plist_xml(
        label             = "com.atlas.digest",
        program_arguments = ["/bin/bash", "scripts/cron.sh"],
        schedule          = {"every_minutes": 15},
        log_path          = "/tmp/x.out.log",
        err_log_path      = "/tmp/x.err.log",
        env               = {"TZ": "America/Los_Angeles", "FOO": "bar"},
    )
    d = _parse(xml)
    assert d["EnvironmentVariables"] == {"TZ": "America/Los_Angeles", "FOO": "bar"}
    # every_minutes → StartInterval in seconds.
    assert d["StartInterval"] == 15 * 60
    assert "StartCalendarInterval" not in d


def test_plist_xml_emits_user_name_and_group_name_when_set() -> None:
    """System-domain LaunchDaemons need UserName/GroupName to run as the
    bot user — without them the daemon runs as root. This is the
    architecturally critical block for service-only bot installs."""
    xml = _build_command_plist_xml(
        label             = "com.atlas.digest",
        program_arguments = ["/bin/bash", "scripts/cron.sh"],
        schedule          = {"every_minutes": 5},
        log_path          = "/tmp/x.out",
        err_log_path      = "/tmp/x.err",
        user_name         = "atlas",
        group_name        = "staff",
    )
    d = _parse(xml)
    assert d["UserName"] == "atlas"
    assert d["GroupName"] == "staff"


def test_plist_xml_group_name_defaults_to_staff_when_user_name_set() -> None:
    """If a caller sets user_name but forgets group_name, default to staff
    rather than emit an invalid plist with UserName alone."""
    xml = _build_command_plist_xml(
        label             = "com.atlas.digest",
        program_arguments = ["/bin/bash", "scripts/cron.sh"],
        schedule          = {"every_minutes": 5},
        log_path          = "/tmp/x.out",
        err_log_path      = "/tmp/x.err",
        user_name         = "atlas",
    )
    d = _parse(xml)
    assert d["UserName"] == "atlas"
    assert d["GroupName"] == "staff"


def test_plist_xml_omits_user_group_when_unset() -> None:
    """Without user_name, both UserName and GroupName are omitted — the
    plist runs as whoever bootstrapped it (root for system, session user
    for gui domain)."""
    xml = _build_command_plist_xml(
        label             = "com.atlas.digest",
        program_arguments = ["/bin/bash", "scripts/cron.sh"],
        schedule          = {"every_minutes": 5},
        log_path          = "/tmp/x.out",
        err_log_path      = "/tmp/x.err",
    )
    d = _parse(xml)
    assert "UserName" not in d
    assert "GroupName" not in d


def test_plist_xml_escapes_xml_special_chars_in_label() -> None:
    # Defensive: a manifest with a weird label shouldn't break the plist.
    xml = _build_command_plist_xml(
        label             = "com.foo&bar.baz<x>",
        program_arguments = ["/bin/echo", "hello"],
        schedule          = {"every_minutes": 1},
        log_path          = "/tmp/x.out",
        err_log_path      = "/tmp/x.err",
    )
    d = _parse(xml)
    assert d["Label"] == "com.foo&bar.baz<x>"


def test_plist_xml_rejects_empty_program_arguments() -> None:
    with pytest.raises(ValueError, match="non-empty list"):
        _build_command_plist_xml(
            label             = "com.x",
            program_arguments = [],
            schedule          = {"every_minutes": 1},
            log_path          = "/tmp/x.out",
            err_log_path      = "/tmp/x.err",
        )


def test_plist_xml_rejects_non_string_program_arg() -> None:
    with pytest.raises(ValueError, match="must be strings"):
        _build_command_plist_xml(
            label             = "com.x",
            program_arguments = ["/bin/sh", 42],
            schedule          = {"every_minutes": 1},
            log_path          = "/tmp/x.out",
            err_log_path      = "/tmp/x.err",
        )


def test_plist_xml_rejects_invalid_schedule() -> None:
    with pytest.raises(ValueError, match="every_minutes or cron"):
        _build_command_plist_xml(
            label             = "com.x",
            program_arguments = ["/bin/sh"],
            schedule          = {},
            log_path          = "/tmp/x.out",
            err_log_path      = "/tmp/x.err",
        )


def test_plist_xml_rejects_zero_every_minutes() -> None:
    with pytest.raises(ValueError, match=">= 1"):
        _build_command_plist_xml(
            label             = "com.x",
            program_arguments = ["/bin/sh"],
            schedule          = {"every_minutes": 0},
            log_path          = "/tmp/x.out",
            err_log_path      = "/tmp/x.err",
        )


# ── install_launchd_command_action — end-to-end with mocked install_launch_agent ─


def _patch_install_launchd_system_daemon(monkeypatch: pytest.MonkeyPatch) -> mock.MagicMock:
    """Replace ``install_launchd_system_daemon`` with a recording stub.

    Returns the stub so tests can assert on its call args. The real
    installer shells out (cp / chown / launchctl); none of that is
    appropriate to exercise from unit tests, so we capture the
    (bot_id, label, plist_xml) it would receive.
    """
    stub = mock.MagicMock(
        return_value={"ok": True, "artifact": "/Library/LaunchDaemons/x.plist",
                      "error": "", "loaded": True}
    )
    monkeypatch.setattr(install_helpers, "install_launchd_system_daemon", stub)
    return stub


def _patch_network(monkeypatch: pytest.MonkeyPatch, bot_user: str = "atlas") -> None:
    """Stub network + bot-user resolution so the helper can run offline."""
    monkeypatch.setattr(install_helpers, "load_network", lambda: {"bots": {bot_user: {"user": bot_user}}})
    monkeypatch.setattr(install_helpers, "get_bot_user", lambda bot_id, net: bot_user)


def test_install_launchd_command_action_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_network(monkeypatch)
    stub = _patch_install_launchd_system_daemon(monkeypatch)

    result = install_launchd_command_action(
        bot_id="atlas",
        action_id="daily-digest",
        label="com.${bot_id}.atlas-digest",
        command="/bin/bash ${workspace}/scripts/atlas-digest-cron.sh",
        schedule={"cron": {"Hour": 7, "Minute": 0}},
        cwd="${workspace}",
        env={"TZ": "America/Los_Angeles"},
    )

    assert result["ok"] is True
    assert result["loaded"] is True

    # install_launchd_system_daemon received the substituted forms.
    call_args = stub.call_args
    assert call_args.args[0] == "atlas"             # bot_id
    assert call_args.args[1] == "com.atlas.atlas-digest"  # label substituted
    plist_xml = call_args.args[2]
    d = _parse(plist_xml)
    assert d["Label"] == "com.atlas.atlas-digest"
    assert d["ProgramArguments"] == [
        "/bin/bash",
        "/Users/atlas/.openclaw/workspace/scripts/atlas-digest-cron.sh",
    ]
    assert d["WorkingDirectory"] == "/Users/atlas/.openclaw/workspace"
    # App-supplied env is preserved AND a PATH that finds openclaw/node is
    # injected (launchd's default PATH excludes /opt/homebrew/bin → exit 127).
    assert d["EnvironmentVariables"] == {
        "TZ": "America/Los_Angeles",
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
    }
    assert d["StartCalendarInterval"] == {"Hour": 7, "Minute": 0}
    # UserName + GroupName are plumbed through so the system daemon runs
    # as the bot user (not root). This is the architectural fix.
    assert d["UserName"] == "atlas"
    assert d["GroupName"] == "staff"


def test_install_launchd_command_action_returns_error_on_relative_program(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_network(monkeypatch)
    _patch_install_launchd_system_daemon(monkeypatch)

    result = install_launchd_command_action(
        bot_id="atlas",
        action_id="daily-digest",
        label="com.atlas.x",
        command="python3 scripts/foo.py",   # relative — should reject
        schedule={"every_minutes": 5},
    )
    assert result["ok"] is False
    assert "absolute path" in result["error"]


def test_install_launchd_command_action_rejects_missing_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_network(monkeypatch)
    _patch_install_launchd_system_daemon(monkeypatch)

    result = install_launchd_command_action(
        bot_id="atlas",
        action_id="daily-digest",
        label="com.atlas.x",
        command="/bin/bash run.sh",
        schedule={},
    )
    assert result["ok"] is False
    assert "schedule" in result["error"]


def test_install_launchd_command_action_rejects_label_with_slash_after_substitution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Defense-in-depth: even a substituted label is rechecked for slashes.
    _patch_network(monkeypatch, bot_user="bad/user")
    _patch_install_launchd_system_daemon(monkeypatch)

    result = install_launchd_command_action(
        bot_id="atlas",
        action_id="x",
        label="com.${bot_id}.foo",  # would substitute to "com.atlas.foo" — fine
        command="/bin/echo hi",
        schedule={"every_minutes": 1},
    )
    # With bot_id "atlas" the label is fine; this is the happy path.
    assert result["ok"] is True

    # But if a manifest author writes a label with a literal slash, reject.
    result_bad = install_launchd_command_action(
        bot_id="atlas",
        action_id="x",
        label="com.atlas/with-slash",
        command="/bin/echo hi",
        schedule={"every_minutes": 1},
    )
    assert result_bad["ok"] is False
    assert "must not contain '/'" in result_bad["error"]


def test_install_launchd_command_action_env_substitution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_network(monkeypatch)
    stub = _patch_install_launchd_system_daemon(monkeypatch)

    install_launchd_command_action(
        bot_id="atlas",
        action_id="x",
        label="com.atlas.x",
        command="/bin/echo hi",
        schedule={"every_minutes": 1},
        env={"WORKSPACE_DIR": "${workspace}", "BOT": "${bot_id}"},
    )
    plist_xml = stub.call_args.args[2]
    d = _parse(plist_xml)
    assert d["EnvironmentVariables"] == {
        "WORKSPACE_DIR": "/Users/atlas/.openclaw/workspace",
        "BOT": "atlas",
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
    }


def test_install_launchd_command_action_injects_openclaw_path_when_no_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core fix: an app cron declaring NO env still gets a PATH that finds
    openclaw/node (else a bare `openclaw` call dies exit 127 under launchd's
    minimal default PATH — silent non-delivery, atlas 2026-06-22)."""
    _patch_network(monkeypatch)
    stub = _patch_install_launchd_system_daemon(monkeypatch)
    install_launchd_command_action(
        bot_id="atlas",
        action_id="daily-digest",
        label="com.atlas.atlas-digest",
        command="/bin/bash /Users/atlas/.openclaw/workspace/scripts/atlas-digest-cron.sh",
        schedule={"cron": {"Hour": 7, "Minute": 0}},
    )
    d = _parse(stub.call_args.args[2])
    assert d["EnvironmentVariables"]["PATH"] == "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"


def test_ensure_launchd_openclaw_path_merges_app_path() -> None:
    from evolve_admin.applications.install_helpers import _ensure_launchd_openclaw_path
    # No env → just the openclaw dirs.
    assert _ensure_launchd_openclaw_path(None)["PATH"] == (
        "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    # App-supplied PATH is kept and takes precedence; openclaw dirs appended.
    merged = _ensure_launchd_openclaw_path({"PATH": "/custom/bin"})["PATH"]
    assert merged == "/custom/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    # A PATH that already lists a dir doesn't duplicate it.
    again = _ensure_launchd_openclaw_path({"PATH": "/opt/homebrew/bin:/x"})["PATH"]
    assert again == "/opt/homebrew/bin:/x:/usr/local/bin:/usr/bin:/bin"


def test_ensure_launchd_openclaw_path_tracks_platform_profile() -> None:
    """The injected dirs come from ``platform_profile.exec_path_dirs`` — the
    SAME source the infra gateway daemon uses — not a parallel hardcoded macOS
    tuple. So on a Linux pod the Homebrew prefix is absent (the NodeSource node
    lands in /usr/bin) and the app-cron PATH matches the platform profile."""
    import platform_profile
    from evolve_admin.applications.install_helpers import _ensure_launchd_openclaw_path

    # macOS (the conftest-pinned default): Homebrew prefixes present, and the
    # injected PATH is exactly the profile's exec dirs joined with ":".
    macos = platform_profile.MACOS
    platform_profile.set_profile(macos)
    try:
        assert _ensure_launchd_openclaw_path(None)["PATH"] == ":".join(macos.exec_path_dirs)
        assert "/opt/homebrew/bin" in _ensure_launchd_openclaw_path(None)["PATH"]
    finally:
        platform_profile.set_profile(macos)

    # Linux: the profile drops /opt/homebrew/bin, so the injected PATH tracks it.
    linux = platform_profile.LINUX
    platform_profile.set_profile(linux)
    try:
        path = _ensure_launchd_openclaw_path(None)["PATH"]
        assert path == ":".join(linux.exec_path_dirs)
        assert path == "/usr/local/bin:/usr/bin:/bin"
        assert "/opt/homebrew/bin" not in path
        # App-supplied PATH is still kept and takes precedence on either OS.
        merged = _ensure_launchd_openclaw_path({"PATH": "/custom/bin"})["PATH"]
        assert merged == "/custom/bin:/usr/local/bin:/usr/bin:/bin"
    finally:
        # Restore the suite's pinned default (conftest's autouse fixture also
        # resets to MACOS, but be explicit so this test can't leak Linux state).
        platform_profile.set_profile(macos)


# ── install_launchd_system_daemon — subprocess shape ──────────────────────────


def _patch_subprocess_run(monkeypatch: pytest.MonkeyPatch) -> mock.MagicMock:
    """Record every subprocess.run call. Returns a stub returning ok by
    default; tests can override return_value to simulate failures.

    Captures (cp, chown, chmod, launchctl bootout, launchctl bootstrap)
    so we can assert on the ordered sequence.
    """
    stub = mock.MagicMock(return_value=mock.MagicMock(returncode=0, stderr="", stdout=""))
    monkeypatch.setattr(install_helpers.subprocess, "run", stub)
    return stub


def test_install_launchd_system_daemon_writes_to_library_launchdaemons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plist lands at /Library/LaunchDaemons/<label>.plist (not
    ~/Library/LaunchAgents/), bootstraps into system domain (not gui/<uid>),
    and chowns to root:wheel (not bot_user:staff) — the three things that
    distinguish a system daemon from a user agent."""
    from evolve_admin.applications.install_helpers import install_launchd_system_daemon

    _patch_network(monkeypatch)
    monkeypatch.setattr(install_helpers, "_bot_uid", lambda u: 510)
    sub = _patch_subprocess_run(monkeypatch)

    result = install_launchd_system_daemon(
        bot_id="atlas",
        label="com.atlas.atlas-daily-digest",
        plist_xml="<?xml version='1.0'?><plist><dict/></plist>",
    )

    assert result["ok"] is True
    assert result["artifact"] == "/Library/LaunchDaemons/com.atlas.atlas-daily-digest.plist"

    # Inspect the subprocess calls. Order: cp → chown root:wheel → chmod 644
    # → launchctl bootout → launchctl bootstrap.
    argvs = [c.args[0] for c in sub.call_args_list]
    assert any(a[:2] == ["sudo", "/bin/cp"]
               and a[-1] == "/Library/LaunchDaemons/com.atlas.atlas-daily-digest.plist"
               for a in argvs)
    assert any(a[:2] == ["sudo", "/usr/sbin/chown"] and a[2] == "root:wheel" for a in argvs)
    assert any(a[:2] == ["sudo", "/bin/chmod"] and a[2] == "644" for a in argvs)
    assert any(a[:3] == ["sudo", "/bin/launchctl", "bootout"]
               and a[3] == "system/com.atlas.atlas-daily-digest" for a in argvs)
    assert any(a[:3] == ["sudo", "/bin/launchctl", "bootstrap"]
               and a[3] == "system" for a in argvs)


def test_install_launchd_system_daemon_rejects_label_with_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evolve_admin.applications.install_helpers import install_launchd_system_daemon
    _patch_network(monkeypatch)
    result = install_launchd_system_daemon(
        bot_id="atlas", label="com.atlas/x", plist_xml="<?xml?>",
    )
    assert result["ok"] is False
    assert "must not contain '/'" in result["error"]


def test_install_launchd_system_daemon_skips_bootstrap_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``bootstrap=False`` (test path), only cp/chown/chmod run —
    no launchctl call. Used by tests that exercise file-only install."""
    from evolve_admin.applications.install_helpers import install_launchd_system_daemon
    _patch_network(monkeypatch)
    monkeypatch.setattr(install_helpers, "_bot_uid", lambda u: 510)
    sub = _patch_subprocess_run(monkeypatch)

    install_launchd_system_daemon(
        bot_id="atlas", label="com.atlas.x", plist_xml="<?xml?>",
        bootstrap=False,
    )
    argvs = [c.args[0] for c in sub.call_args_list]
    assert not any("launchctl" in a[1] for a in argvs if len(a) > 1)


def test_install_launchd_system_daemon_propagates_bootstrap_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If launchctl bootstrap fails (e.g. malformed plist), the plist
    file is still on disk but ``loaded=False`` so the caller can
    surface the gap without losing the work."""
    from evolve_admin.applications.install_helpers import install_launchd_system_daemon
    _patch_network(monkeypatch)
    monkeypatch.setattr(install_helpers, "_bot_uid", lambda u: 510)

    def fake_run(argv, **kwargs):
        if len(argv) >= 3 and argv[1] == "/bin/launchctl" and argv[2] == "bootstrap":
            return mock.MagicMock(returncode=1, stderr="malformed plist", stdout="")
        return mock.MagicMock(returncode=0, stderr="", stdout="")
    monkeypatch.setattr(install_helpers.subprocess, "run", fake_run)

    result = install_launchd_system_daemon(
        bot_id="atlas", label="com.atlas.x", plist_xml="<?xml?>",
    )
    assert result["ok"] is True   # the file landed
    assert result["loaded"] is False   # but the bootstrap failed


# ── repair_app_cron_env_paths — fleet self-heal (2026-06-22) ─────────────────


class _FakeManifest:
    def __init__(self, actions):
        self.scheduled_actions = actions


def _digest_action(plist_path: str) -> dict:
    return {
        "id": "daily-digest", "mechanism": "launchd",
        "installed_artifact": plist_path,
        "install": {
            "plist_label": "ai.evolve.${bot_id}.atlas-daily-digest",
            "command": "/bin/bash /Users/atlas/.openclaw/workspace/scripts/atlas-digest-cron.sh",
            "schedule": {"cron": {"Hour": 7, "Minute": 0}},
        },
    }


def _patch_repair_env(monkeypatch, manifests):
    from pathlib import Path as _Path
    monkeypatch.setattr(install_helpers, "bot_home",
                        lambda b, n=None: _Path("/Users/atlas"))
    import evolve_admin.applications.manifest as _m
    monkeypatch.setattr(_m, "list_manifests", lambda sd, bot: manifests)


def test_repair_heals_app_cron_missing_path(tmp_path, monkeypatch):
    plist = tmp_path / "ai.evolve.atlas.atlas-daily-digest.plist"
    plist.write_text("<plist><dict><key>Label</key><string>x</string></dict></plist>")  # no PATH
    _patch_repair_env(monkeypatch, [_FakeManifest([_digest_action(str(plist))])])
    called = {}

    def _fake_install(*a, **k):
        called["hit"] = (a, k)
        return {"ok": True}

    monkeypatch.setattr(install_helpers, "install_launchd_command_action", _fake_install)
    rep = install_helpers.repair_app_cron_env_paths(["atlas"], network={}, bootstrap=False)
    assert rep["missing"] == ["ai.evolve.atlas.atlas-daily-digest"]
    assert rep["healed"] == ["ai.evolve.atlas.atlas-daily-digest"]
    assert "hit" in called  # re-install was invoked


def test_repair_check_only_reports_without_installing(tmp_path, monkeypatch):
    plist = tmp_path / "ai.evolve.atlas.atlas-daily-digest.plist"
    plist.write_text("<plist><dict></dict></plist>")
    _patch_repair_env(monkeypatch, [_FakeManifest([_digest_action(str(plist))])])
    monkeypatch.setattr(install_helpers, "install_launchd_command_action",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not install")))
    rep = install_helpers.repair_app_cron_env_paths(["atlas"], network={}, check_only=True)
    assert rep["missing"] == ["ai.evolve.atlas.atlas-daily-digest"]
    assert rep["healed"] == []


def test_repair_skips_plist_that_already_has_path(tmp_path, monkeypatch):
    plist = tmp_path / "ai.evolve.atlas.atlas-daily-digest.plist"
    plist.write_text(
        "<plist><dict><key>EnvironmentVariables</key><dict>"
        "<key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin</string></dict></dict></plist>")
    _patch_repair_env(monkeypatch, [_FakeManifest([_digest_action(str(plist))])])
    rep = install_helpers.repair_app_cron_env_paths(["atlas"], network={}, bootstrap=False)
    assert rep["checked"] == 1
    assert rep["missing"] == [] and rep["healed"] == []


def test_repair_skips_uninstalled_action(tmp_path, monkeypatch):
    # installed_artifact points at a plist that doesn't exist → not materialized.
    _patch_repair_env(monkeypatch,
                      [_FakeManifest([_digest_action(str(tmp_path / "absent.plist"))])])
    rep = install_helpers.repair_app_cron_env_paths(["atlas"], network={}, bootstrap=False)
    assert rep["checked"] == 0 and rep["missing"] == []
