"""§4b iMessage keeper grant — rendered per bot user, never a `*` user.

Review pr-4667 second pass, finding 2: a root `cp` into ANY user's
``Library/LaunchAgents/`` plus the existing ``bootstrap gui/*`` grant is code
execution as any GUI user, the admin included. The grant is rendered one
copy + ownership pair per bot account from network.json, destination fixed.
Placeholder names: docs/PLACEHOLDER_NAMING.md.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from platform_profile import LINUX, MACOS, set_profile

from evolve_admin import imessage_channel as ic
from evolve_admin import setup_wizard

MAC_OC_PATH = "/opt/homebrew/lib/node_modules/openclaw/bin/openclaw"
KEEPER_CP = re.compile(r"^evolve ALL=\(root\) NOPASSWD: /bin/cp /tmp/evolve-imsg-keeper-\*\.plist (\S+)$", re.M)
KEEPER_CHOWN = re.compile(r"^evolve ALL=\(root\) NOPASSWD: /usr/sbin/chown (\S+) (\S+imessage-keeper\S+)$", re.M)


def _network(tmp_path: Path, bots: dict) -> Path:
    p = tmp_path / "network.json"
    p.write_text(json.dumps({"bots": bots}))
    return p


def _render(monkeypatch, users=None) -> str:
    set_profile(MACOS)
    monkeypatch.setattr(setup_wizard, "_find_openclaw_path", lambda: MAC_OC_PATH)
    content = setup_wizard._render_evolve_sudoers(keeper_users=users)
    assert content is not None
    return content


def test_roster_is_one_account_per_real_bot(tmp_path):
    net = _network(tmp_path, {
        "personal-bot": {"user": "personal-bot-user", "port": 18801},
        "team-bot-c": {"port": 18802},                      # account == bot id
        "team-bot-a": {"purpose": "planned, no account yet"},  # planned: no grant
        "admin-bot": {"user": "personal-bot-user", "port": 18803},  # shares an account
    })
    assert setup_wizard._imessage_keeper_users(net) == ["personal-bot-user", "team-bot-c"]


def test_an_unreadable_network_json_renders_no_keeper_grant(tmp_path):
    bad = tmp_path / "network.json"
    bad.write_text("{not json")
    assert setup_wizard._imessage_keeper_users(bad) == []


def test_one_copy_grant_per_bot_and_none_with_a_wildcard_user(monkeypatch):
    users = ["personal-bot-user", "team-bot-c", "security-bot"]
    content = _render(monkeypatch, users)
    dests = KEEPER_CP.findall(content)
    # DONE-WHEN: the keeper copy grant count equals the number of bot users.
    assert len(dests) == len(users)
    assert dests == [f"/Users/{u}/Library/LaunchAgents/ai.openclaw.imessage-keeper.{u}.plist" for u in users]
    assert all("*" not in d for d in dests)
    for owner, dest in KEEPER_CHOWN.findall(content):
        assert "*" not in owner and "*" not in dest
    assert "/Users/*/Library/LaunchAgents/ai.openclaw.imessage-keeper" not in content


def test_the_grant_destination_is_the_path_install_keeper_writes(monkeypatch):
    """Argv ↔ grant parity: a drifted byte is a dead grant at 3am."""
    user = "personal-bot-user"
    content = _render(monkeypatch, [user])
    calls = []

    class R:
        returncode, stdout, stderr = 0, "501\n", ""

    def run(argv, **_):
        calls.append(argv)
        return R()

    ok, _ = ic.install_keeper(user, run=run, home_for_user=lambda u: Path("/Users") / u)
    assert ok
    cp = next(c for c in calls if c[1] == "/bin/cp")
    chown = next(c for c in calls if c[1] == "/usr/sbin/chown")
    assert KEEPER_CP.findall(content) == [cp[3]]
    # sudoers needs the colon escaped; sudo matches the unescaped argv.
    assert KEEPER_CHOWN.findall(content) == [(chown[2].replace(":", "\\:"), chown[3])]


def test_a_name_that_is_not_a_plain_short_name_never_reaches_a_grant(monkeypatch):
    content = _render(monkeypatch, ["personal-bot-user", "*", "a b", "x/../admin", "evil\nevolve ALL=(ALL) ALL"])
    assert len(KEEPER_CP.findall(content)) == 1
    assert not re.search(r"^evolve ALL=\(ALL\) ALL$", content, re.M)  # no injected line
    assert content.count("# skipped keeper grant:") == 4


def test_linux_render_carries_no_keeper_grant(monkeypatch):
    set_profile(LINUX)
    monkeypatch.setattr(setup_wizard, "_find_openclaw_path", lambda: "/usr/lib/node_modules/openclaw/bin/openclaw")
    content = setup_wizard._render_evolve_sudoers(keeper_users=["personal-bot-user"])
    assert content is not None and "imessage-keeper" not in content


def _visudo() -> "str | None":
    found = shutil.which("visudo")
    if found:
        return found
    return "/usr/sbin/visudo" if Path("/usr/sbin/visudo").exists() else None


def test_per_bot_render_passes_visudo(monkeypatch, tmp_path):
    visudo = _visudo()
    if visudo is None:
        pytest.skip("no visudo on this host")
    f = tmp_path / "evolve.sudoers"
    f.write_text(_render(monkeypatch, ["personal-bot-user", "team-bot-c"]))
    r = subprocess.run([visudo, "-c", "-f", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
