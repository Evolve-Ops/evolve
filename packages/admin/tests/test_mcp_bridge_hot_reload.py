"""Hot-reload tests for mcp_bridge.registry.BotRegistry.

Regression cover for the Google-setup-wizard gap (2026-06-20): the wizard
wrote ``google_integration`` to network.json and reported "✓ Verified," but
the MCP bridge only learned about a bot through its BotRegistry, which was
populated at startup and refreshed by a 30s poll the wizard never triggered.
A freshly-configured bot therefore failed every Google tool call until a
manual ``launchctl kickstart`` — and the bot confabulated a wrong "OAuth
token expired" diagnosis in the meantime.

The fix makes the registry reload lazily on read access when network.json's
mtime advances, so a config write lands on the *next* tool call with no
restart. These tests exercise:

* a new bot is resolvable on the next ``get()`` — no ``_reload()`` call, no
  poll wait, no restart;
* end-to-end ``tool_gmail_send`` for a bot added *after* startup succeeds;
* a torn/partial network.json keeps last-good state (no crash) and recovers;
* a single malformed bot entry doesn't wipe the rest of the registry.

The background poll is disabled (``poll_interval`` very large) so every pass
proves the *lazy* path, not the poll. mtime is bumped explicitly via
``os.utime`` so the tests don't depend on filesystem mtime granularity.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from unittest.mock import MagicMock, patch

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.mcp_bridge import google_tools  # noqa: E402
from evolve_admin.mcp_bridge import registry as reg_mod  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_net(path: Path, bots: dict, *, bump: bool = True) -> None:
    """Write a network.json and force a strictly-increasing mtime.

    Bumping mtime explicitly (rather than trusting the write to advance it)
    makes the lazy-reload trigger deterministic regardless of the host
    filesystem's mtime resolution (HFS+ is 1s; APFS is ns).
    """
    prev = path.stat().st_mtime if path.exists() else 0.0
    path.write_text(json.dumps({"bots": bots}, indent=2))
    if bump:
        os.utime(path, (prev + 10, prev + 10))


def _gi(bot_id: str) -> dict:
    """A minimal Path-C google_integration block for ``bot_id``."""
    return {
        "mode": "service_account_dwd",
        "workspace_domain": "example-corp.com",
        "subject": f"{bot_id}@example-corp.com",
        "service_account_secret_ref": "google-sa-example-corp",
        "scopes": ["https://www.googleapis.com/auth/gmail.send"],
    }


@pytest.fixture
def registry_factory(tmp_path, monkeypatch):
    """Build real BotRegistry instances with workspace resolution stubbed.

    ``get_bot_workspace`` normally reads the *default* network.json and
    resolves a real macOS account — neither exists in a test env — so we
    patch it (and ``_bot_home``) on the registry module to return tmp paths.
    A per-bot ``raises`` set lets a test exercise the skip-one-bad-entry path.
    """
    raises: set[str] = set()

    def fake_ws(bot_id, *_a, **_k):
        if bot_id in raises:
            raise RuntimeError(f"no such user for {bot_id}")
        return tmp_path / bot_id / "workspace"

    monkeypatch.setattr(reg_mod, "get_bot_workspace", fake_ws)
    monkeypatch.setattr(reg_mod, "_bot_home", lambda bot_id, net=None: tmp_path / bot_id)

    created: list = []

    def _make(net_path: Path):
        reg = reg_mod.BotRegistry(network_path=net_path, poll_interval=10_000)
        created.append(reg)
        return reg

    _make.raises = raises  # type: ignore[attr-defined]
    yield _make
    for reg in created:
        reg.stop()


# ── Lazy reload: a wizard-configured bot lands on the next tool call ──────────

def test_new_bot_resolvable_on_next_call_without_restart(tmp_path, registry_factory):
    net_path = tmp_path / "network.json"
    _write_net(net_path, {"lex": {"role": "member"}}, bump=False)
    reg = registry_factory(net_path)

    assert reg.get("lex") is not None
    assert reg.get("team_bot_a") is None  # not configured yet

    # The wizard writes a freshly-configured Path-C bot.
    _write_net(net_path, {
        "lex": {"role": "member"},
        "team_bot_a": {"role": "member", "google_integration": _gi("team_bot_a")},
    })

    # Next tool call resolves it — no _reload() call, no poll tick, no restart.
    info = reg.get("team_bot_a")
    assert info is not None
    assert info.bot_id == "team_bot_a"
    assert "team_bot_a" in reg.bot_ids()


def test_lazy_reload_does_not_refire_when_mtime_unchanged(tmp_path, registry_factory, monkeypatch):
    """The fast path is a stat + float compare — no reload when mtime is flat."""
    net_path = tmp_path / "network.json"
    _write_net(net_path, {"lex": {"role": "member"}}, bump=False)
    reg = registry_factory(net_path)

    calls = {"n": 0}
    real_reload = reg._reload

    def counting_reload(*a, **k):
        calls["n"] += 1
        return real_reload(*a, **k)

    monkeypatch.setattr(reg, "_reload", counting_reload)
    # Several reads, no file change → zero reloads.
    for _ in range(5):
        reg.get("lex")
    assert calls["n"] == 0


# ── Change callbacks fire on membership change, outside the reload lock ───────

def test_change_callback_fires_and_can_reenter_read_accessor(tmp_path, registry_factory):
    """A change callback that itself calls a read accessor must not deadlock.

    Callbacks fire after _reload_lock is released, so re-entering get()/
    bot_ids() (which call _maybe_reload) is safe. If callbacks fired while
    the reload lock were held, this would hang on the non-reentrant lock.
    """
    net_path = tmp_path / "network.json"
    _write_net(net_path, {"lex": {"role": "member"}}, bump=False)
    reg = registry_factory(net_path)

    seen: list = []

    def on_change(added, removed):
        # Re-enter a read accessor from inside the callback.
        ids = reg.bot_ids()
        seen.append((sorted(added), sorted(removed), sorted(ids)))

    reg.on_change(on_change)

    _write_net(net_path, {
        "lex": {"role": "member"},
        "team_bot_a": {"role": "member", "google_integration": _gi("team_bot_a")},
    })
    # Triggers the lazy reload + callback; must return (not hang).
    assert reg.get("team_bot_a") is not None
    assert len(seen) == 1
    added, removed, ids = seen[0]
    assert added == ["team_bot_a"]
    assert removed == []
    assert "team_bot_a" in ids and "lex" in ids


# ── Torn / partial writes don't crash the bridge ─────────────────────────────

def test_torn_network_json_keeps_last_good_and_recovers(tmp_path, registry_factory):
    net_path = tmp_path / "network.json"
    _write_net(net_path, {"lex": {"role": "member"}}, bump=False)
    reg = registry_factory(net_path)
    assert reg.get("lex") is not None

    # A reader races the wizard's non-atomic write and sees a half-written file.
    prev = net_path.stat().st_mtime
    net_path.write_text('{"bots": {"lex": {"role":')  # truncated, invalid JSON
    os.utime(net_path, (prev + 10, prev + 10))

    # Must not raise and must keep the last-good view.
    assert reg.get("lex") is not None
    assert reg.bot_ids() == ["lex"]

    # The writer finishes; the next read recovers and sees the new bot.
    _write_net(net_path, {
        "lex": {"role": "member"},
        "team_bot_a": {"role": "member", "google_integration": _gi("team_bot_a")},
    })
    assert reg.get("team_bot_a") is not None


def test_malformed_bot_entry_does_not_wipe_registry(tmp_path, registry_factory):
    net_path = tmp_path / "network.json"
    # "broken" resolves to a non-existent account → workspace lookup raises.
    registry_factory.raises.add("broken")  # type: ignore[attr-defined]
    _write_net(net_path, {
        "lex": {"role": "member"},
        "broken": {"role": "member"},
    }, bump=False)
    reg = registry_factory(net_path)

    assert reg.get("lex") is not None  # good bot survives
    assert reg.get("broken") is None   # bad entry skipped, didn't abort reload


# ── End-to-end: the exact path that failed live now works after a write ──────

def test_gmail_tool_works_for_bot_configured_after_startup(tmp_path, registry_factory):
    """Build a registry with one bot; add a Google-configured bot; the very
    next gmail_send call resolves it and succeeds — no bridge restart.

    This is the live-incident path end to end: _resolve_bot (registry) +
    _bot_cfg (config.load_network) must both reflect the post-startup write.
    """
    net_path = tmp_path / "network.json"
    _write_net(net_path, {"lex": {"role": "member"}}, bump=False)
    reg = registry_factory(net_path)

    # Wizard configures a brand-new Path-C bot after the bridge started.
    _write_net(net_path, {
        "lex": {"role": "member"},
        "team_bot_a": {
            "role": "member",
            "primary_user": {"name": "Sam Riley"},
            "google_integration": _gi("team_bot_a"),
        },
    })

    fake_send = MagicMock(return_value=MagicMock(execute=MagicMock(
        return_value={"id": "sent-1", "threadId": "t-1"}
    )))
    fake_messages = MagicMock(send=MagicMock(return_value=fake_send.return_value))
    fake_users = MagicMock(messages=MagicMock(return_value=fake_messages))
    fake_service = MagicMock(users=MagicMock(return_value=fake_users))

    # _bot_cfg reads config.load_network() fresh — point it at the temp file
    # so the tool sees the same just-written config the registry does.
    def _load_temp_net(*_a, **_k):
        return json.loads(net_path.read_text())

    with patch("evolve_admin.mcp_bridge.google_tools.config.load_network",
               side_effect=_load_temp_net), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.assert_scopes_available",
               return_value=None), \
         patch("evolve_admin.mcp_bridge.google_tools.google_auth.load_credentials",
               return_value=MagicMock()), \
         patch("evolve_admin.google_service._build_service",
               return_value=fake_service):
        result = asyncio.new_event_loop().run_until_complete(
            google_tools.tool_gmail_send(
                {"bot": "team_bot_a", "to": "x@y.com", "subject": "Hi", "body": "Hello"},
                reg,
            )
        )

    assert result["ok"] is True
    assert result["id"] == "sent-1"
    assert result["bot"] == "team_bot_a"
