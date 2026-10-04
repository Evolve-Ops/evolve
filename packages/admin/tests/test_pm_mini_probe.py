"""Unit tests for tools/pm-mini-probe — the PM's read-only pod probe.

Brief: internal/dispatch/done/pm-mini-probe-readonly-mcp.md.

This server hands a language model an ``ssh`` to a production pod. Everything
that keeps that safe is in one file and is tested here, case by case:

  * the allowlist is **deny-by-default** and each refusal names the rule that
    caught it — so a widening is a visible diff, not a silent behaviour change;
  * no key bytes leave the pod (masking on a fixture that carries every key
    shape the pod actually stores);
  * every call is recorded, and a call that cannot be recorded does not run;
  * the MCP handshake works end to end against a fake ``ssh`` on PATH.

The tool is an extensionless script under tools/, so we load it by path.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parents[3] / "tools" / "pm-mini-probe"


def _load_tool():
    loader = importlib.machinery.SourceFileLoader("pm_mini_probe", str(_TOOL))
    spec = importlib.util.spec_from_loader("pm_mini_probe", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pm_mini_probe"] = mod
    loader.exec_module(mod)
    return mod


MOD = _load_tool()


# ── 1. The allowlist table ───────────────────────────────────────────────────
#
# Each row is (command, expected allow-rule). If any of these stops passing,
# the PM lost a measurement it depends on.

ALLOWED = [
    # The brief's own acceptance line.
    ("readlink -f /Users/Shared/evolve-venv/bin/python3", "read-tool"),
    # Bot config reads — the one that needs the root reader.
    ("sudo cat /Users/team-bot-a/.openclaw/openclaw.json", "read-tool"),
    # Shared-dir forensics.
    ("ls /Users/Shared/evolve/signals/firing", "read-tool"),
    ("wc -l /Users/Shared/evolve/watchdog/2026-09-05.jsonl", "read-tool"),
    ("tail -n 40 /Users/Shared/evolve/signals/log/2026-09-05.jsonl", "read-tool"),
    ("head -c 400 /Users/Shared/evolve/proposals/pending/abc123.json", "read-tool"),
    ("grep firing /Users/Shared/evolve/signals/log/2026-09-05.jsonl", "read-tool"),
    ("stat -f %Sm /Users/Shared/evolve-repo/CLAUDE.md", "read-tool"),
    ("du -sh /Users/Shared/evolve/proposals", "read-tool"),
    # Quoted grep pattern with a space — legitimate, single command.
    ('grep "cost event" /var/log/install.log', "read-tool"),
    # Daemon state.
    ("launchctl print system/ai.evolve.evolve.admin-ui", "read-tool"),
    ("cat /Library/LaunchDaemons/ai.evolve.evolve.admin-ui.plist", "read-tool"),
    # Process shape — no path operands at all.
    ("ps aux", "read-tool"),
    ("pgrep -fl gateway", "read-tool"),
    ("id", "read-tool"),
    # Analyzer measurement tools — unprivileged, which is the only way they run.
    (
        "python3 /Users/Shared/evolve-repo/packages/analyzer/context_census.py --bot team-bot-a --days 3",
        "analyzer-script",
    ),
    (
        "python3 /Users/Shared/evolve-repo/packages/analyzer/context_census.py --bot team-bot-a --json",
        "analyzer-script",
    ),
    # A script the PM staged via the operator.
    ("python3 /tmp/pm/turns_slice.py --shared-dir=/Users/Shared/evolve", "analyzer-script"),
    # evolve-admin reads.
    ("sudo evolve-admin ensure-pod-perms --check-only", "evolve-admin-read"),
    ("sudo evolve-admin health", "evolve-admin-read"),
    ("sudo evolve-admin release status", "evolve-admin-read"),
    ("evolve-admin breaker status", "evolve-admin-read"),
    ("sudo evolve-admin health --json", "evolve-admin-read"),
    ("sudo evolve-admin audit-acls --json", "evolve-admin-read"),
    # Verbs whose required positional cannot be written into sudoers without a
    # wildcard run unprivileged — with the bot id their parser demands.
    ("evolve-admin lifecycle inventory team-bot-a", "evolve-admin-read"),
    ("evolve-admin list-rollbacks --bot team-bot-a --json", "evolve-admin-read"),
    ("evolve-admin list-rollback-points team-bot-a --limit 5", "evolve-admin-read"),
    # Firewall posture (the security track's two questions).
    ("/usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate", "firewall-state"),
    ("/usr/libexec/ApplicationFirewall/socketfilterfw --listapps", "firewall-state"),
]

REFUSED = [
    # Chaining / substitution / redirection — one command per call.
    ("ls /Users/Shared/evolve && rm -rf /tmp/pm", "metachar"),
    ("cat /Users/Shared/evolve/network.json; id", "metachar"),
    ("cat /Users/Shared/evolve/network.json | tee /tmp/pm/x", "metachar"),
    ("cat /Users/Shared/evolve/$(whoami)", "metachar"),
    ("cat /Users/Shared/evolve/`whoami`", "metachar"),
    ("cat /Users/Shared/evolve/network.json > /tmp/pm/x", "metachar"),
    ("cat /Users/Shared/evolve/a\ncat /etc/passwd", "metachar"),
    # Path escapes.
    ("cat /Users/Shared/evolve/../../etc/passwd", "path-traversal"),
    ("ls /Users/Shared/evolve/signals/..", "path-traversal"),
    ("cat /etc/passwd", "path-outside-allowlist"),
    # Credential-shaped operands INSIDE the path set — the deny rule ahead of
    # masking. (Outside the set, the wider fact wins: see the .ssh row below.)
    ("cat /Users/team-bot-a/.openclaw/agents/main/agent/auth-profiles.json", "secret-path"),
    ("sudo cat /Users/team-bot-a/.openclaw/agents/main/agent/auth-profiles.json", "secret-path"),
    ("cat /Users/Shared/evolve/keystore/machine.key", "secret-path"),
    ("cat /Users/Shared/evolve/credentials.json", "secret-path"),
    ("cat /Users/Shared/evolve-repo/.env", "secret-path"),
    ("cat /Users/Shared/evolve-repo/docs/private/notes.md", "secret-path"),
    ("head -c 400 /Users/Shared/evolve/keystore/admin-auth.pem", "secret-path"),
    ("cat /Users/Shared/evolveX/secrets.json", "path-outside-allowlist"),
    ("cat /Users/team-bot-a/.ssh/id_ed25519", "path-outside-allowlist"),
    ("ls /", "path-outside-allowlist"),
    ("cat network.json", "relative-path"),
    # Writers, however spelled.
    ("rm -rf /Users/Shared/evolve", "mutating-binary"),
    ("mv /Users/Shared/evolve/a /Users/Shared/evolve/b", "mutating-binary"),
    ("cp /Users/Shared/evolve/a /tmp/pm/b", "mutating-binary"),
    ("sudo chown evolve /Users/Shared/evolve", "mutating-binary"),
    ("sudo chmod 777 /Users/Shared/evolve", "mutating-binary"),
    ("tee /tmp/pm/x", "mutating-binary"),
    ("sqlite3 /Users/team-bot-a/.openclaw/x.sqlite", "mutating-binary"),
    ("bash /tmp/pm/script.sh", "mutating-binary"),
    # Service control.
    ("launchctl bootout system/ai.evolve.evolve.admin-ui", "launchctl-nonprint"),
    ("launchctl kickstart -k system/ai.evolve.evolve.oc-gateway", "launchctl-nonprint"),
    # evolve-admin beyond the read set.
    ("sudo evolve-admin board token team-bot-a", "evolve-admin-mutating"),
    ("sudo evolve-admin deploy team-bot-a", "evolve-admin-mutating"),
    ("sudo evolve-admin refresh-sudoers", "evolve-admin-mutating"),
    ("sudo evolve-admin release rollback", "evolve-admin-mutating"),
    # ensure-pod-perms WITHOUT --check-only applies changes.
    ("sudo evolve-admin ensure-pod-perms", "evolve-admin-mutating"),
    # git.
    ("git reset --hard origin/main", "git-mutating"),
    ("git push origin main", "git-mutating"),
    ("git status", "not-allowlisted"),
    # openclaw, at all.
    ("openclaw models", "openclaw-forbidden"),
    ("/opt/homebrew/bin/openclaw config get", "openclaw-forbidden"),
    ("pgrep -fl openclaw", "openclaw-forbidden"),
    # Interpreters and scripts outside the set.
    ("python3 /Users/Shared/evolve-repo/tools/preflight", "analyzer-script"),
    ("python3 /Users/team-bot-a/.openclaw/evil.py", "analyzer-script"),
    (
        "python3 /Users/Shared/evolve-repo/packages/analyzer/sub/dir/x.py",
        "analyzer-script",
    ),
    # No interpreter is granted root, whichever one is named and whichever
    # script it is handed: `context_census.py` needs a free --bot value, so no
    # exact form can be written into sudoers without the wildcard that used to
    # hand root `--json-out /etc/anything`.
    (
        "sudo python3 /Users/Shared/evolve-repo/packages/analyzer/context_census.py",
        "analyzer-script",
    ),
    (
        "sudo /usr/bin/python3 /Users/Shared/evolve-repo/packages/analyzer/audit.py",
        "analyzer-script",
    ),
    # Output flags are writes.
    (
        "python3 /Users/Shared/evolve-repo/packages/analyzer/context_census.py --json-out /tmp/pm/x.json",
        "write-flag",
    ),
    ("ls --output /tmp/pm/x", "write-flag"),
    # sudo is granted for `cat` only among the read tools.
    ("sudo ls /Users/Shared/evolve", "read-tool"),
    ("sudo launchctl print system/ai.evolve.evolve.admin-ui", "read-tool"),
    # `sudo cat` is the root reader, which takes exactly one operand and no
    # flags. The probe mirrors that contract so the refusal is legible here.
    ("sudo cat /Users/Shared/evolve/a /Users/Shared/evolve/b", "read-tool"),
    ("sudo cat -n /Users/Shared/evolve/network.json", "read-tool"),
    # No interpreter is granted root at all.
    (
        "sudo /usr/bin/python3 /Users/Shared/evolve-repo/packages/analyzer/context_census.py --json",
        "analyzer-script",
    ),
    # Under sudo the WHOLE evolve-admin form must be one the sudoers file writes
    # out; an ungranted one would prompt for a password and return silence.
    ("sudo evolve-admin lifecycle inventory team-bot-a", "evolve-admin-sudo-ungranted"),
    ("sudo evolve-admin health --verbose", "evolve-admin-sudo-ungranted"),
    ("sudo evolve-admin list-rollbacks --bot team-bot-a", "evolve-admin-sudo-ungranted"),
    # Flags are pinned per verb / per script, read off their own parsers.
    ("evolve-admin breaker status --since 3d", "evolve-admin-unknown-flag"),
    (
        "python3 /Users/Shared/evolve-repo/packages/analyzer/context_census.py --cache-dir /tmp/pm",
        "analyzer-unknown-flag",
    ),
    # `file -C` writes magic.mgc into the remote cwd, so `file` is not a read tool.
    ("file /Users/Shared/evolve/network.json", "not-allowlisted"),
    # readlink without -f resolves nothing useful and is not the granted shape.
    ("readlink /Users/Shared/evolve-venv/bin/python3", "read-tool"),
    # A bare reader hangs on stdin.
    ("cat", "read-tool"),
    ("grep firing", "read-tool"),
    # socketfilterfw with anything else.
    ("/usr/libexec/ApplicationFirewall/socketfilterfw --setglobalstate off", "firewall-state"),
    # Nothing at all.
    ("", "empty-command"),
    ("   ", "empty-command"),
    ('cat "/Users/Shared/evolve/a', "unparseable"),
    # Not a shape we know.
    ("nc -l 4242", "not-allowlisted"),
    ("ssh other-host id", "not-allowlisted"),
]


@pytest.mark.parametrize("cmd,rule", ALLOWED, ids=[c[:60] for c, _ in ALLOWED])
def test_allowed_shapes_pass(cmd: str, rule: str) -> None:
    assert MOD.classify(cmd) == rule


@pytest.mark.parametrize("cmd,rule", REFUSED, ids=[c[:60] or "empty" for c, _ in REFUSED])
def test_refused_shapes_name_their_rule(cmd: str, rule: str) -> None:
    with pytest.raises(MOD.Refused) as exc:
        MOD.classify(cmd)
    assert exc.value.rule == rule, (
        f"{cmd!r} was refused by {exc.value.rule!r}, expected {rule!r}: {exc.value.detail}"
    )


def test_allowlist_is_deny_by_default() -> None:
    """A shape nobody thought about is refused, not admitted."""
    for cmd in ("frobnicate --all", "/usr/bin/true", "cat -", "sudo -i"):
        with pytest.raises(MOD.Refused):
            MOD.classify(cmd)


def test_every_refusal_carries_a_rule_and_a_reason() -> None:
    """The PM has to be able to tell WHY, or it will guess and retry blindly."""
    for cmd, _ in REFUSED:
        with pytest.raises(MOD.Refused) as exc:
            MOD.classify(cmd)
        assert exc.value.rule and exc.value.detail
        assert exc.value.rule in str(exc.value)


# ── 2. Secret masking ────────────────────────────────────────────────────────

FIXTURE_OUTPUT = """{
  "anthropic": {"apiKey": "sk-ant-api03-AAAAAAAAAAAABBBBBBBBBBBBCCCCCCCCCCCCDDDD"},
  "openai": {"apiKey": "sk-proj-ZZZZZZZZZZZZZZZZYYYYYYYYYYYYYYYY"},
  "xai": {"apiKey": "xai-QQQQQQQQQQQQQQQQRRRRRRRRRRRRRRRR"},
  "google": {"apiKey": "AIzaSyDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD"},
  "github": {"token": "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"},
  "github_fine": {"token": "github_pat_11ABCDEFG0abcdefghij_KLMNOPQRSTUVWXYZ0123456789"},
  "session": {"secret": "bXlzdXBlcnNlY3JldHNlc3Npb25rZXl0aGF0aXN2ZXJ5bG9uZ2luZGVlZA=="}
}
"""

_LITERAL_SECRETS = [
    "sk-ant-api03-AAAAAAAAAAAABBBBBBBBBBBBCCCCCCCCCCCCDDDD",
    "sk-proj-ZZZZZZZZZZZZZZZZYYYYYYYYYYYYYYYY",
    "xai-QQQQQQQQQQQQQQQQRRRRRRRRRRRRRRRR",
    "AIzaSyDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD",
    "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "github_pat_11ABCDEFG0abcdefghij_KLMNOPQRSTUVWXYZ0123456789",
    "bXlzdXBlcnNlY3JldHNlc3Npb25rZXl0aGF0aXN2ZXJ5bG9uZ2luZGVlZA==",
]


def test_masking_removes_every_key_shape_the_pod_stores() -> None:
    masked = MOD.mask_secrets(FIXTURE_OUTPUT)
    for secret in _LITERAL_SECRETS:
        assert secret not in masked, f"{secret[:12]}… survived masking"
    assert masked.count("***MASKED***") == len(_LITERAL_SECRETS)


def test_masking_keeps_the_first_8_and_last_4_so_a_key_stays_identifiable() -> None:
    masked = MOD.mask_secrets("key = " + _LITERAL_SECRETS[0])
    assert "sk-ant-a***MASKED***DDDD" in masked


def test_masking_leaves_ordinary_text_alone() -> None:
    """A slug ending in `sk-` must not be mangled into unreadable output."""
    text = (
        "branch claude/task-abcdefghijklmnopqrs is green\n"
        "timeout=30 shared-dir=/Users/Shared/evolve\n"
    )
    assert MOD.mask_secrets(text) == text


def test_masking_is_idempotent() -> None:
    once = MOD.mask_secrets(FIXTURE_OUTPUT)
    assert MOD.mask_secrets(once) == once


# ── 3. Audit — one line per call, and no call without one ────────────────────


@pytest.fixture()
def probe_env(tmp_path, monkeypatch):
    """Point the probe at a temp audit log and a fake ssh that echoes its cmd."""
    log = tmp_path / "pm" / "mini-probe.log"
    monkeypatch.setenv("PM_PROBE_LOG", str(log))
    fake = tmp_path / "fake-ssh"
    fake.write_text('#!/bin/sh\necho "ran: $4"\nexit 0\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PM_PROBE_SSH", str(fake))
    monkeypatch.setenv("PM_PROBE_HOST", "test-pod")
    return log


def _lines(log: Path) -> list[dict]:
    return [json.loads(x) for x in log.read_text().splitlines() if x.strip()]


def test_allowed_call_is_audited_without_its_output(probe_env) -> None:
    out = MOD.run("readlink -f /Users/Shared/evolve-venv/bin/python3")
    assert out["exit"] == 0
    assert "ran: readlink -f" in out["stdout"]
    entries = _lines(probe_env)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["cmd"] == "readlink -f /Users/Shared/evolve-venv/bin/python3"
    assert entry["exit"] == 0
    assert entry["bytes"] > 0
    assert "refused_by" not in entry
    # The log records that a call happened, never what came back.
    assert "ran:" not in json.dumps(entry)


def test_refused_call_is_audited_with_its_rule(probe_env) -> None:
    out = MOD.run("rm -rf /Users/Shared/evolve")
    assert out["rule"] == "mutating-binary"
    assert "error" in out
    entries = _lines(probe_env)
    assert len(entries) == 1
    assert entries[0]["refused_by"] == "mutating-binary"
    assert entries[0]["exit"] is None


def test_every_call_appends_exactly_one_line(probe_env) -> None:
    MOD.run("id")
    MOD.run("rm -rf /tmp/pm")
    MOD.run("ls /Users/Shared/evolve")
    assert len(_lines(probe_env)) == 3


def test_run_refuses_when_the_audit_log_is_unwritable(tmp_path, monkeypatch) -> None:
    """Silence is not an option: no log, no call."""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)
    monkeypatch.setenv("PM_PROBE_LOG", str(blocked / "sub" / "mini-probe.log"))
    monkeypatch.setenv("PM_PROBE_SSH", "/bin/false")
    try:
        out = MOD.run("id")
    finally:
        blocked.chmod(0o700)
    assert out["rule"] == "audit-unwritable"
    assert "not writable" in out["error"]
    assert "stdout" not in out


def test_unwritable_log_blocks_even_an_allowed_command(tmp_path, monkeypatch) -> None:
    """The ssh must not have run — the fake would have left a marker if it had."""
    marker = tmp_path / "ran"
    fake = tmp_path / "fake-ssh"
    fake.write_text(f'#!/bin/sh\ntouch {marker}\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)
    monkeypatch.setenv("PM_PROBE_LOG", str(blocked / "sub" / "log"))
    monkeypatch.setenv("PM_PROBE_SSH", str(fake))
    try:
        MOD.run("id")
    finally:
        blocked.chmod(0o700)
    assert not marker.exists()


# ── 4. Output handling ───────────────────────────────────────────────────────


def test_output_is_capped_and_flagged(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PM_PROBE_LOG", str(tmp_path / "log"))
    fake = tmp_path / "fake-ssh"
    fake.write_text(f"#!/bin/sh\nhead -c {MOD.MAX_OUTPUT_BYTES * 2} /dev/zero | tr '\\0' 'a'\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PM_PROBE_SSH", str(fake))
    out = MOD.run("ls /Users/Shared/evolve")
    assert out["truncated"] is True
    assert len(out["stdout"].encode()) == MOD.MAX_OUTPUT_BYTES


def test_output_is_masked_before_it_is_returned(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PM_PROBE_LOG", str(tmp_path / "log"))
    fake = tmp_path / "fake-ssh"
    fake.write_text(f'#!/bin/sh\ncat <<\'EOF\'\n{FIXTURE_OUTPUT}EOF\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PM_PROBE_SSH", str(fake))
    out = MOD.run("sudo cat /Users/team-bot-a/.openclaw/openclaw.json")
    for secret in _LITERAL_SECRETS:
        assert secret not in out["stdout"]


def test_timeout_is_clamped(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PM_PROBE_LOG", str(tmp_path / "log"))
    monkeypatch.setenv("PM_PROBE_SSH", "/usr/bin/true")
    captured: dict = {}

    def fake_run(argv, **kw):
        captured["timeout"] = kw["timeout"]
        captured["argv"] = argv
        raise OSError("stopped here")

    monkeypatch.setattr(MOD.subprocess, "run", fake_run)
    MOD.run("id", timeout_s=99999)
    assert captured["timeout"] == MOD.MAX_TIMEOUT_S
    MOD.run("id", timeout_s=0)
    assert captured["timeout"] == MOD.MIN_TIMEOUT_S


def test_ssh_argv_is_batchmode_and_carries_one_command(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PM_PROBE_LOG", str(tmp_path / "log"))
    monkeypatch.setenv("PM_PROBE_HOST", "test-pod")
    captured: dict = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        raise OSError("stopped here")

    monkeypatch.setattr(MOD.subprocess, "run", fake_run)
    MOD.run("id")
    assert captured["argv"][1:] == ["-o", "BatchMode=yes", "test-pod", "id"]


# ── 4b. `sudo cat` is rewritten to the pod's root reader ────────────────────


def test_sudo_cat_is_rewritten_to_the_fixed_argv_reader() -> None:
    assert MOD.remote_command("sudo cat /Users/Shared/evolve/network.json") == (
        f"sudo {MOD.PM_PROBE_CAT} /Users/Shared/evolve/network.json"
    )


def test_a_path_with_a_space_survives_the_rewrite_quoted() -> None:
    """The rewrite rebuilds the command, so it owns the quoting it destroys."""
    out = MOD.remote_command('sudo cat "/Users/Shared/evolve/a b.json"')
    assert out == f"sudo {MOD.PM_PROBE_CAT} '/Users/Shared/evolve/a b.json'"


def test_unprivileged_commands_are_sent_verbatim() -> None:
    for cmd in ("cat /Users/Shared/evolve/network.json", "id", "sudo evolve-admin health"):
        assert MOD.remote_command(cmd) == cmd


def test_run_sends_the_rewritten_command_and_logs_both(probe_env) -> None:
    out = MOD.run("sudo cat /Users/Shared/evolve/network.json")
    assert out["remote"] == f"sudo {MOD.PM_PROBE_CAT} /Users/Shared/evolve/network.json"
    # The fake ssh echoes its 4th argv element — proof of what was sent.
    assert MOD.PM_PROBE_CAT in out["stdout"]
    entry = _lines(probe_env)[0]
    assert entry["cmd"] == "sudo cat /Users/Shared/evolve/network.json"
    assert entry["remote"] == out["remote"]


# ── 5. The MCP handshake, end to end ─────────────────────────────────────────


def _speak(messages: list[dict], env_extra: dict) -> list[dict]:
    """Drive the server over stdio and return one response per request."""
    env = dict(os.environ)
    env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, str(_TOOL)],
        input="".join(json.dumps(m) + "\n" for m in messages),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


@pytest.fixture()
def stdio_env(tmp_path):
    """A fake `ssh` first on PATH, plus a temp audit log."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "ssh"
    fake.write_text('#!/bin/sh\necho "pod says: $4"\nexit 0\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return {
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        "PM_PROBE_LOG": str(tmp_path / "pm" / "mini-probe.log"),
        "PM_PROBE_HOST": "test-pod",
        "PM_PROBE_SSH": "",
    }


def test_initialize_and_tools_list_expose_exactly_one_tool(stdio_env) -> None:
    responses = _speak(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ],
        stdio_env,
    )
    # The notification gets no response — two requests, two responses.
    assert [r["id"] for r in responses] == [1, 2]
    init = responses[0]["result"]
    assert init["serverInfo"]["name"] == "pm-mini-probe"
    assert "tools" in init["capabilities"]
    tools = responses[1]["result"]["tools"]
    assert len(tools) == 1
    tool = tools[0]
    assert tool["name"] == "run"
    assert tool["inputSchema"]["required"] == ["cmd"]
    assert set(tool["inputSchema"]["properties"]) == {"cmd", "timeout_s"}
    assert tool["inputSchema"]["additionalProperties"] is False


def test_tools_call_round_trips_against_a_fake_ssh_on_path(stdio_env) -> None:
    responses = _speak(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "run", "arguments": {
                 "cmd": "readlink -f /Users/Shared/evolve-venv/bin/python3"}}},
        ],
        stdio_env,
    )
    call = responses[1]["result"]
    assert call["isError"] is False
    payload = json.loads(call["content"][0]["text"])
    assert payload["exit"] == 0
    assert payload["host"] == "test-pod"
    assert "pod says: readlink -f /Users/Shared/evolve-venv/bin/python3" in payload["stdout"]
    assert Path(stdio_env["PM_PROBE_LOG"]).exists()


def test_tools_call_reports_a_refusal_as_an_error_result_not_a_crash(stdio_env) -> None:
    responses = _speak(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "run", "arguments": {"cmd": "rm -rf /Users/Shared/evolve"}}},
        ],
        stdio_env,
    )
    call = responses[1]["result"]
    assert call["isError"] is True
    payload = json.loads(call["content"][0]["text"])
    assert payload["rule"] == "mutating-binary"


def test_unknown_tool_and_unknown_method_are_jsonrpc_errors(stdio_env) -> None:
    responses = _speak(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "shell", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "resources/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        ],
        stdio_env,
    )
    assert responses[0]["error"]["code"] == -32602
    assert responses[1]["error"]["code"] == -32601
    assert responses[2]["result"] == {}


def test_malformed_line_does_not_wedge_the_transport(stdio_env) -> None:
    env = dict(os.environ)
    env.update(stdio_env)
    proc = subprocess.run(
        [sys.executable, str(_TOOL)],
        input='not json\n{"jsonrpc": "2.0", "id": 7, "method": "ping"}\n',
        capture_output=True, text=True, env=env, timeout=60,
    )
    responses = [json.loads(x) for x in proc.stdout.splitlines() if x.strip()]
    assert responses[0]["error"]["code"] == -32700
    assert responses[1]["id"] == 7


# ── 6. The two enumerations must not drift apart ─────────────────────────────


def test_no_interpreter_is_granted_root_on_either_side() -> None:
    """The probe refuses `sudo <python>`, and the render grants no interpreter.

    Either half alone would be a lie: a grant the probe never uses is a hole
    an operator's shell can still walk through, and a probe shape the grant
    does not cover prompts for a password over a BatchMode ssh — which comes
    back as silence, not as an error the PM can read."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from evolve_admin.pm_probe_sudoers import render_pm_probe_sudoers

    assert not hasattr(MOD, "SUDO_ANALYZER_SCRIPTS")
    rendered = render_pm_probe_sudoers("pod-admin")
    grants = [ln for ln in rendered.splitlines() if " NOPASSWD: " in ln]
    assert not [ln for ln in grants if "python" in ln.split(" NOPASSWD: ", 1)[1]]
    for cmd in (
        "sudo python3 /Users/Shared/evolve-repo/packages/analyzer/context_census.py --bot b",
        "sudo /usr/bin/python3 /Users/Shared/evolve-repo/packages/analyzer/context_census.py",
        "sudo /Users/Shared/evolve-venv/bin/python3 /tmp/pm/x.py",
    ):
        with pytest.raises(MOD.Refused) as exc:
            MOD.classify(cmd)
        assert exc.value.rule == "analyzer-script"


def test_sudo_evolve_admin_forms_match_the_sudoers_render_exactly() -> None:
    """Both sides enumerate the same argument tuples, or one of them is wrong.

    The render writes each form out in full — no trailing `*`, which sudo
    would match against any tail (`audit-acls --apply` was one token away)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from evolve_admin.pm_probe_sudoers import PM_PROBE_EVOLVE_ADMIN_READS

    granted = {tuple(form.split()) for form in PM_PROBE_EVOLVE_ADMIN_READS}
    assert MOD.SUDO_EVOLVE_ADMIN_FORMS == granted


def test_every_sudo_form_names_a_known_read_verb() -> None:
    for form in MOD.SUDO_EVOLVE_ADMIN_FORMS:
        words = tuple(w for w in form if not w.startswith("-"))
        assert words in MOD.EVOLVE_ADMIN_READS, f"{form} names no read verb"


def test_flag_table_covers_every_read_verb() -> None:
    """A verb with no flag entry would raise KeyError on its first flag."""
    assert set(MOD.EVOLVE_ADMIN_FLAGS) == set(MOD.EVOLVE_ADMIN_READS)


def test_no_rendered_grant_carries_a_wildcard_argument() -> None:
    """The finding this branch exists for: sudo's `*` crosses `/` and spaces,
    so a wildcard in an argument position is `any argument string`, not a
    bounded subtree."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from evolve_admin.pm_probe_sudoers import render_pm_probe_sudoers

    for ln in render_pm_probe_sudoers("pod-admin").splitlines():
        if " NOPASSWD: " not in ln:
            continue
        assert "*" not in ln.split(" NOPASSWD: ", 1)[1], ln


def test_secret_globs_match_the_pod_side_reader() -> None:
    """Refusing on the laptop is the fast path; refusing as root is the one
    that holds if anything ever reaches the pod by another route."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from evolve_admin import pm_probe_cat

    assert MOD.SECRET_SEGMENT_GLOBS == pm_probe_cat.SECRET_SEGMENT_GLOBS
    assert MOD.SECRET_DIR_PAIR == pm_probe_cat.SECRET_DIR_PAIR
    assert MOD.PM_PROBE_CAT == pm_probe_cat.INSTALL_PATH
    assert MOD.MAX_OUTPUT_BYTES == pm_probe_cat.MAX_OUTPUT_BYTES
