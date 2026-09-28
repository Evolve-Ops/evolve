"""Tests for the ``disable_channel_in_oc_config`` helper.

Added 2026-06-04 alongside the Skills-page Uninstall affordance + the
telegram/slack/discord symmetric-revoke fix (closes deep-audit
2026-05-30 F2 — asymmetric revoke left channels.<id> + plugins.entries.<id>
dangling in openclaw.json, which the Skills page then surfaced as a
configured-but-broken nag the operator had no way to dismiss).

The helper itself is shape-agnostic — it knows nothing about which
channel it's tearing down, only that the contract is "make channels.<id>
and (optionally) plugins.entries.<id> not exist."
"""

from __future__ import annotations

from evolve_admin.skills import _oc_install_common as _oc_common


class TestDisableChannelInOcConfig:

    def test_removes_channels_and_plugin_entry(self, monkeypatch):
        existing = {
            "channels": {
                "telegram": {"botToken": "12345:abcdef"},
                "slack":    {"botToken": "xoxb-keep-me"},  # untouched
            },
            "plugins": {
                "entries": {
                    "telegram": {"enabled": True},
                    "slack":    {"enabled": True},  # untouched
                },
            },
        }
        captured = {}
        monkeypatch.setattr(_oc_common, "read_oc_config",
                            lambda bot_id: (existing, None))
        def _w(bot_id, cfg):
            captured["cfg"] = cfg
            return True, None
        monkeypatch.setattr(_oc_common, "write_oc_config", _w)

        ok, err = _oc_common.disable_channel_in_oc_config("team-bot-a", "telegram")
        assert ok and err is None

        cfg = captured["cfg"]
        assert "telegram" not in cfg["channels"]
        assert "telegram" not in cfg["plugins"]["entries"]
        # Other channels untouched
        assert cfg["channels"]["slack"]["botToken"] == "xoxb-keep-me"
        assert cfg["plugins"]["entries"]["slack"]["enabled"] is True

    def test_idempotent_when_nothing_to_remove(self, monkeypatch):
        """The Skills-page Uninstall button can fire against a bot whose
        state was already cleared by a prior call (or that never had the
        channel in the first place). Helper returns ok without rewriting
        openclaw.json — avoids burning a sudo cp + bumping mtime for no
        state change."""
        existing = {"channels": {}, "plugins": {"entries": {}}}
        writes = {"count": 0}
        monkeypatch.setattr(_oc_common, "read_oc_config",
                            lambda bot_id: (existing, None))
        def _w(bot_id, cfg):
            writes["count"] += 1
            return True, None
        monkeypatch.setattr(_oc_common, "write_oc_config", _w)

        ok, err = _oc_common.disable_channel_in_oc_config("team-bot-a", "telegram")
        assert ok and err is None
        assert writes["count"] == 0, "no-op clean path should not call write_oc_config"

    def test_remove_plugin_entry_false_keeps_entry(self, monkeypatch):
        """Some callers want to clear channels.<id> but keep the plugin
        entry on (e.g. for a different bot's account still using it).
        Default is True; explicit False preserves the plugin entry."""
        existing = {
            "channels": {"telegram": {"botToken": "12345:abcdef"}},
            "plugins": {"entries": {"telegram": {"enabled": True}}},
        }
        captured = {}
        monkeypatch.setattr(_oc_common, "read_oc_config",
                            lambda bot_id: (existing, None))
        def _w(bot_id, cfg):
            captured["cfg"] = cfg
            return True, None
        monkeypatch.setattr(_oc_common, "write_oc_config", _w)

        ok, err = _oc_common.disable_channel_in_oc_config(
            "team-bot-a", "telegram", remove_plugin_entry=False,
        )
        assert ok and err is None
        assert "telegram" not in captured["cfg"]["channels"]
        assert captured["cfg"]["plugins"]["entries"]["telegram"]["enabled"] is True

    def test_read_failure_propagates_without_writing(self, monkeypatch):
        write_called = {"v": False}
        monkeypatch.setattr(_oc_common, "read_oc_config",
                            lambda bot_id: (None, "ENOENT"))
        monkeypatch.setattr(_oc_common, "write_oc_config",
                            lambda bot_id, cfg: write_called.__setitem__("v", True))
        ok, err = _oc_common.disable_channel_in_oc_config("team-bot-a", "telegram")
        assert ok is False
        assert "ENOENT" in (err or "")
        assert write_called["v"] is False

    def test_channel_present_but_no_plugin_entry_still_clears(self, monkeypatch):
        """Mirrors the documented 2026-06-04 legacy-orphan shape:
        ``channels.whatsapp`` exists but ``plugins.entries.whatsapp`` doesn't.
        Helper should still clear the channel block without erroring on the
        missing plugin entry."""
        existing = {
            "channels": {
                "whatsapp": {"enabled": False, "dmPolicy": "pairing"},
            },
            "plugins": {"entries": {}},
        }
        captured = {}
        monkeypatch.setattr(_oc_common, "read_oc_config",
                            lambda bot_id: (existing, None))
        def _w(bot_id, cfg):
            captured["cfg"] = cfg
            return True, None
        monkeypatch.setattr(_oc_common, "write_oc_config", _w)

        ok, err = _oc_common.disable_channel_in_oc_config("team-bot-a", "whatsapp")
        assert ok and err is None
        assert "whatsapp" not in captured["cfg"]["channels"]

    def test_plugin_entry_present_but_no_channel_still_clears(self, monkeypatch):
        """Inverse residue: plugin entry present, channel block missing.
        Helper still clears the plugin entry."""
        existing = {
            "channels": {},
            "plugins": {"entries": {"telegram": {"enabled": True}}},
        }
        captured = {}
        monkeypatch.setattr(_oc_common, "read_oc_config",
                            lambda bot_id: (existing, None))
        def _w(bot_id, cfg):
            captured["cfg"] = cfg
            return True, None
        monkeypatch.setattr(_oc_common, "write_oc_config", _w)

        ok, err = _oc_common.disable_channel_in_oc_config("team-bot-a", "telegram")
        assert ok and err is None
        assert "telegram" not in captured["cfg"]["plugins"]["entries"]
