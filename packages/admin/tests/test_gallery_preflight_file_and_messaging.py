"""tests/test_gallery_preflight_file_and_messaging.py

The two requirement types the gallery framework audit (2026-07-02, S6)
found the requirements[] schema could not express, leaving preflight
all-green on dead-on-arrival apps:

  • ``requirements.files[]`` — a bot-home-relative file the app is dead
    without (pm-inbox's ``~/.openclaw/pm-inbox-github-tokens.json``,
    mode 0600), with an optional exact-mode check.

  • ``requirements.messaging_channel[]`` — "this bot can send its person
    a message", the prerequisite six delivery apps assumed in build_spec
    prose only. Resolution mirrors the delivery convention's
    ``resolve_route`` (channels.*.enabled in openclaw.json ∪ the
    network.json primary_user route) via the SHARED readers
    (``evolve_admin.channels``, ``read_oc_config``) — no third fork.

Adversarial cases covered per the S6 chip brief:
  • a ``file`` requirement pointing outside bot-home via ``../`` or an
    absolute path is REFUSED (state "unknown" — which also makes the
    conformance gate fail any shipped package that tries it);
  • ``messaging_channel`` must NOT false-positive on a channel that is
    present but ``enabled: false`` — even when a delivery route is
    recorded in network.json (the send would fail loudly every window);
  • unreadable-home / unreadable-config degrades: file existence falls
    back to ``sudo /bin/test`` (fail-closed without a grant); channel
    state falls back to trusting the recorded route (the convention's
    own degrade).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ADMIN = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN))

from evolve_admin.applications import gallery
from evolve_admin.applications.gallery import (
    check_file_requirement,
    check_messaging_channel_requirement,
    preflight_check,
)


class _StubCompleted:
    returncode = 1
    stdout = ""
    stderr = ""


@pytest.fixture()
def no_sudo(monkeypatch):
    """Fail every subprocess call — sudo fallbacks come back 'not found'."""
    calls: list[list] = []

    def fake_run(argv, *a, **k):
        calls.append(list(argv))
        return _StubCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


@pytest.fixture()
def bot_home(monkeypatch, tmp_path):
    """Point gallery._home_of at a temp dir standing in for the bot home."""
    home = tmp_path / "home" / "testbot"
    home.mkdir(parents=True)
    monkeypatch.setattr(gallery, "_home_of", lambda bot_id: home)
    return home


# ─── check_file_requirement ──────────────────────────────────────────────────


def _file_req(**over):
    req = {
        "id": "tokens-file",
        "path": ".openclaw/pm-inbox-github-tokens.json",
        "display_name": "Tokens file",
        "required": True,
    }
    req.update(over)
    return req


def test_file_satisfied_when_present(bot_home, no_sudo):
    p = bot_home / ".openclaw/pm-inbox-github-tokens.json"
    p.parent.mkdir(parents=True)
    p.write_text("{}")
    state, msg = check_file_requirement("testbot", _file_req())
    assert state == "satisfied"
    assert ".openclaw/pm-inbox-github-tokens.json" in msg


def test_file_missing_when_absent(bot_home, no_sudo):
    state, msg = check_file_requirement("testbot", _file_req())
    assert state == "missing"
    assert "not found" in msg
    # The sudo /bin/test fallback was consulted before giving up.
    assert any(c[:2] == ["sudo", "/bin/test"] for c in no_sudo)


def test_file_tilde_prefix_accepted(bot_home, no_sudo):
    p = bot_home / ".openclaw/pm-inbox-github-tokens.json"
    p.parent.mkdir(parents=True)
    p.write_text("{}")
    state, _ = check_file_requirement(
        "testbot", _file_req(path="~/.openclaw/pm-inbox-github-tokens.json")
    )
    assert state == "satisfied"


def test_file_mode_match(bot_home, no_sudo):
    p = bot_home / "tokens.json"
    p.write_text("{}")
    p.chmod(0o600)
    state, msg = check_file_requirement(
        "testbot", _file_req(path="tokens.json", mode="0600")
    )
    assert state == "satisfied"
    assert "0600" in msg


def test_file_mode_mismatch_is_missing(bot_home, no_sudo):
    p = bot_home / "tokens.json"
    p.write_text("{}")
    p.chmod(0o644)
    state, msg = check_file_requirement(
        "testbot", _file_req(path="tokens.json", mode="0600")
    )
    assert state == "missing"
    assert "0644" in msg and "0600" in msg


def test_file_mode_unverifiable_degrades_to_satisfied(bot_home, no_sudo, monkeypatch):
    """File exists but stat is denied → satisfied with a note, never a
    false flag on a healthy bot (pre-ACL homes)."""
    p = bot_home / "tokens.json"
    p.write_text("{}")

    real_stat = Path.stat

    def deny_stat(self, **kw):
        if self.name == "tokens.json":
            raise PermissionError(str(self))
        return real_stat(self, **kw)

    monkeypatch.setattr(Path, "stat", deny_stat)
    # is_file() uses stat under the hood → the direct probe fails too, so
    # make the sudo /bin/test fallback answer "exists".
    class _Zero:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Zero())

    state, msg = check_file_requirement(
        "testbot", _file_req(path="tokens.json", mode="0600")
    )
    assert state == "satisfied"
    assert "unverified" in msg


@pytest.mark.parametrize("path", [
    "../../../etc/sudoers",
    "/etc/sudoers",
    "~/../pod-admin-user/.ssh/id_rsa",
    "~root/.ssh/id_rsa",
    "..",
])
def test_file_traversal_and_absolute_paths_refused(bot_home, no_sudo, path):
    """Attack case: a package must not be able to point the (potentially
    sudo-backed) existence probe outside the installing bot's home."""
    state, msg = check_file_requirement("testbot", _file_req(path=path))
    assert state == "unknown"
    # No probe of any kind may have run for the refused path.
    assert not no_sudo


def test_file_symlink_escape_refused(bot_home, no_sudo, tmp_path):
    """Attack case: a symlink component inside .openclaw must not redirect
    the probe outside the bot home (the lexical guards can't catch this —
    the escape only appears once the path resolves on disk)."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("x")
    oc = bot_home / ".openclaw"
    oc.mkdir(parents=True)
    (oc / "esc").symlink_to(outside)
    state, msg = check_file_requirement(
        "testbot", _file_req(path=".openclaw/esc/secret")
    )
    assert state == "unknown"
    assert "escape" in msg
    # The sudo fallback must never have been reached for the escaping path.
    assert not any(c[:2] == ["sudo", "/bin/test"] for c in no_sudo)


def test_file_symlink_inside_home_allowed(bot_home, no_sudo):
    """A symlink whose target stays under the home resolves clean."""
    real = bot_home / "data"
    real.mkdir()
    (real / "tokens.json").write_text("{}")
    (bot_home / "link").symlink_to(real)
    state, _ = check_file_requirement(
        "testbot", _file_req(path="link/tokens.json")
    )
    assert state == "satisfied"


def test_file_empty_path_unknown(bot_home, no_sudo):
    state, _ = check_file_requirement("testbot", _file_req(path=""))
    assert state == "unknown"


def test_file_bad_mode_string_unknown(bot_home, no_sudo):
    p = bot_home / "tokens.json"
    p.write_text("{}")
    state, msg = check_file_requirement(
        "testbot", _file_req(path="tokens.json", mode="rw-------")
    )
    assert state == "unknown"
    assert "octal" in msg


# ─── check_messaging_channel_requirement ─────────────────────────────────────


def _patch_oc(monkeypatch, cfg):
    """Patch the shared openclaw.json reader the checker resolves lazily."""
    from evolve_admin.skills import _oc_install_common

    if cfg is None:
        monkeypatch.setattr(
            _oc_install_common, "read_oc_config",
            lambda bot_id: (None, "oc_read_failed: unreadable"),
        )
    else:
        monkeypatch.setattr(
            _oc_install_common, "read_oc_config", lambda bot_id: (cfg, None)
        )


def _patch_network(monkeypatch, external_ids):
    from evolve_admin import config as _cfg

    monkeypatch.setattr(
        _cfg, "load_network",
        lambda *a, **k: {
            "bots": {"testbot": {"primary_user": {"external_ids": external_ids}}}
        },
    )


_MC_REQ = {"id": "messaging_channel", "required": True}


def test_mc_satisfied_on_enabled_channel_with_route(monkeypatch):
    _patch_oc(monkeypatch, {"channels": {"telegram": {"enabled": True, "botToken": "x"}}})
    _patch_network(monkeypatch, {"telegram": "12345"})
    state, msg = check_messaging_channel_requirement("testbot", _MC_REQ)
    assert state == "satisfied"
    assert "telegram" in msg
    assert "route" in msg


def test_mc_satisfied_on_enabled_channel_without_route_notes_skip(monkeypatch):
    _patch_oc(monkeypatch, {"channels": {"slack": {"enabled": True}}})
    _patch_network(monkeypatch, {})
    state, msg = check_messaging_channel_requirement("testbot", _MC_REQ)
    assert state == "satisfied"
    assert "skipped" in msg  # honest note: no recorded route yet


def test_mc_disabled_channel_never_satisfies_even_with_route(monkeypatch):
    """Adversarial case: channels.telegram present but enabled:false, and a
    route IS recorded — the convention's send would fail loudly. Missing."""
    _patch_oc(monkeypatch, {"channels": {"telegram": {"enabled": False, "botToken": "x"}}})
    _patch_network(monkeypatch, {"telegram": "12345"})
    state, msg = check_messaging_channel_requirement("testbot", _MC_REQ)
    assert state == "missing"
    assert "not enabled" in msg


def test_mc_missing_when_nothing_configured(monkeypatch):
    _patch_oc(monkeypatch, {"channels": {}})
    _patch_network(monkeypatch, {})
    state, msg = check_messaging_channel_requirement("testbot", _MC_REQ)
    assert state == "missing"
    assert "Connect a channel" in msg


def test_mc_unreadable_config_trusts_recorded_route(monkeypatch):
    """openclaw.json unreadable + recorded route → satisfied, mirroring the
    delivery convention's enabled_channels()→None degrade."""
    _patch_oc(monkeypatch, None)
    _patch_network(monkeypatch, {"telegram": "12345"})
    state, msg = check_messaging_channel_requirement("testbot", _MC_REQ)
    assert state == "satisfied"
    assert "unreadable" in msg


def test_mc_unreadable_config_no_route_missing(monkeypatch):
    _patch_oc(monkeypatch, None)
    _patch_network(monkeypatch, {})
    state, _ = check_messaging_channel_requirement("testbot", _MC_REQ)
    assert state == "missing"


def test_mc_non_messaging_route_key_ignored_when_unreadable(monkeypatch):
    """openclaw.json unreadable but the only recorded external_ids key is a
    non-messaging id → NOT satisfied. The degrade trusts a recorded *route*,
    but only on a channel the convention's resolve_route would actually
    walk (its CHANNEL_PRIORITY / MESSAGING_INTEGRATION_IDS vocabulary)."""
    _patch_oc(monkeypatch, None)
    _patch_network(monkeypatch, {"carrier-pigeon": "coop-7"})
    state, _ = check_messaging_channel_requirement("testbot", _MC_REQ)
    assert state == "missing"


def test_mc_non_messaging_channel_does_not_satisfy(monkeypatch):
    """A channel outside the coherence gate's messaging vocabulary (e.g. a
    webhook-ish entry) must not count as a messaging channel."""
    _patch_oc(monkeypatch, {"channels": {"webhook": {"enabled": True}}})
    _patch_network(monkeypatch, {})
    state, _ = check_messaging_channel_requirement("testbot", _MC_REQ)
    assert state == "missing"


# ─── preflight_check wiring ──────────────────────────────────────────────────


def _wired_pkg(**req_over):
    reqs = {
        "integrations": [],
        "secrets": [],
        "system": [],
        "python_packages": [],
        "files": [
            {
                "id": "tokens-file",
                "path": ".openclaw/tokens.json",
                "display_name": "Tokens file",
                "mode": "0600",
                "required": True,
            }
        ],
        "messaging_channel": [{"id": "messaging_channel", "required": True}],
    }
    reqs.update(req_over)
    return {
        "pkg_id": "p-00000001",
        "id": "app_wired_test",
        "name": "wired-test",
        "display_name": "Wired Test",
        "objective": "test",
        "build_spec": "test",
        "app_dependencies": [],
        "requirements": reqs,
    }


@pytest.fixture()
def wired(monkeypatch, bot_home, no_sudo):
    """preflight_check on a synthetic package carrying both new types."""
    def run(pkg):
        monkeypatch.setattr(gallery, "load_gallery_package", lambda *a, **k: pkg)
        return preflight_check(pkg["pkg_id"], "testbot", Path("/nonexistent-shared"))
    # Default surrounding state: nothing configured anywhere.
    _patch_oc(monkeypatch, {"channels": {}})
    _patch_network(monkeypatch, {})
    return run


def test_preflight_rows_and_gap_when_both_missing(wired):
    result = wired(_wired_pkg())
    rows = {r["type"]: r for r in result["requirements"]}
    assert rows["file"]["state"] == "missing"
    assert rows["file"]["id"] == "tokens-file"
    assert rows["file"]["severity"] == "runtime_warning"
    assert rows["messaging_channel"]["state"] == "missing"
    assert rows["messaging_channel"]["severity"] == "runtime_warning"
    # runtime_warning, not build_blocker: still buildable, not runnable.
    assert result["ready_to_build"] is True
    assert result["ready_to_run"] is False


def test_preflight_satisfied_when_present(wired, bot_home, monkeypatch):
    p = bot_home / ".openclaw/tokens.json"
    p.parent.mkdir(parents=True)
    p.write_text("{}")
    p.chmod(0o600)
    _patch_oc(monkeypatch, {"channels": {"telegram": {"enabled": True}}})
    result = wired(_wired_pkg())
    rows = {r["type"]: r for r in result["requirements"]}
    assert rows["file"]["state"] == "satisfied"
    assert rows["messaging_channel"]["state"] == "satisfied"
    assert result["ready_to_run"] is True


def test_preflight_file_build_blocker_escalation(wired):
    pkg = _wired_pkg()
    pkg["requirements"]["files"][0]["severity"] = "build_blocker"
    result = wired(pkg)
    rows = {r["type"]: r for r in result["requirements"]}
    assert rows["file"]["severity"] == "build_blocker"
    assert result["ready_to_build"] is False


def test_preflight_optional_entries_are_info_and_dont_gap(wired, monkeypatch):
    pkg = _wired_pkg(
        files=[{
            "id": "optional-file", "path": "opt.json", "required": False,
        }],
        messaging_channel=[{"id": "messaging_channel", "required": False}],
    )
    result = wired(pkg)
    rows = {r["type"]: r for r in result["requirements"]}
    assert rows["file"]["severity"] == "info"
    assert rows["messaging_channel"]["severity"] == "info"
    assert result["ready_to_run"] is True


def test_preflight_traversal_row_is_unknown_and_gaps(wired):
    pkg = _wired_pkg(
        files=[{"id": "evil", "path": "../../etc/sudoers", "required": True}],
        messaging_channel=[],
    )
    result = wired(pkg)
    rows = {r["type"]: r for r in result["requirements"]}
    assert rows["file"]["state"] == "unknown"
    assert result["ready_to_run"] is False


# ─── the stamped shipped packages parse through the real checkers ────────────


_STAMPED = [
    ("p-5f2bc54c", "pm-inbox"),          # files + messaging_channel
    ("p-a9a74bf7", "morning-briefing"),
    ("p-1d3e8f47", "evening-sweep"),
    ("p-2c7a9b6e", "pre-meeting-brief"),
    ("p-41e4c5f4", "email-triage"),
    ("p-738f057c", "calendar-summary"),
]


@pytest.mark.parametrize("pkg_id,slug", _STAMPED)
def test_stamped_packages_full_preflight(pkg_id, slug, bot_home, no_sudo, monkeypatch, tmp_path):
    """Each stamped package runs the FULL preflight and its new-type rows
    come back with real ids and definite states (the conformance gate's
    parseability sweep, asserted here per-package with the state visible)."""
    _patch_oc(monkeypatch, {"channels": {}})
    _patch_network(monkeypatch, {})
    result = preflight_check(pkg_id, "testbot", tmp_path)
    assert "error" not in result
    by_type: dict[str, list] = {}
    for r in result["requirements"]:
        by_type.setdefault(r["type"], []).append(r)
    mc_rows = by_type.get("messaging_channel") or []
    assert len(mc_rows) == 1, f"{slug}: expected one messaging_channel row"
    assert mc_rows[0]["id"] == "messaging_channel"
    assert mc_rows[0]["state"] in ("satisfied", "missing")
    if slug == "pm-inbox":
        file_rows = by_type.get("file") or []
        assert len(file_rows) == 1
        assert file_rows[0]["id"] == "pm-inbox-github-tokens"
        assert file_rows[0]["state"] in ("satisfied", "missing")
