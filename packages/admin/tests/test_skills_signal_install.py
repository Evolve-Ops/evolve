"""Tests for evolve_admin.skills.signal_install.

**LICENSING NOTE:** the signal_install module ships behind a licensing
review gate (see its docstring). The tests below are pure logic — no
signal-cli or libsignal code is invoked — so they remain useful even if
the licensing review eventually withdraws the catalog entry. They also
serve as regression coverage for the QR pairing + bundled-plugin
patterns that Matrix and other Phase-1 channels will reuse.

Coverage:
- TestE164Validation
- TestResolveStatus         — every state-machine branch + F3 rule
- TestBuildInstallPlan      — each state maps to right ordered step list
- TestCaptureNumber         — number validation + placeholder write
- TestEnableAccountInOcConfig — write semantics + operator-field preservation
- TestRevokeAccount         — symmetric revoke + multi-account behaviour
- TestAccessPanelHonest     — Plex-test + F5 no-aspirational-promises
- TestSkillRegistryEntry    — UI hooks
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.skills import signal_install as si  # noqa: E402


# ── Canonical "fully wired" openclaw.json shape ──────────────────────────────

_NUMBER = "+15551234567"
_OK_OC_CFG = {
    "plugins": {
        "installs": {si.SIGNAL_PLUGIN_NPM: {"version": "2026.6.1"}},
        "entries": {"signal": {"enabled": True}},
    },
    "channels": {
        "signal": {
            "enabled": True,
            "accounts": {
                _NUMBER: {
                    "enabled": True,
                    "number": _NUMBER,
                    "configDir": "/Users/bot-user-a/.openclaw/signal/config/15551234567",
                    "deviceName": "evolve-team-bot-a",
                },
            },
        },
    },
}


def _resolve(
    *,
    oc_cfg: dict | None = None,
    oc_err: str | None = None,
    config_dir_populated: bool = True,
    probe: dict | None = None,
):
    if oc_cfg is None and oc_err is None:
        oc_cfg = _OK_OC_CFG
    if probe is None:
        probe = {"connected": True}
    return si.resolve_status(
        "team-bot-a",
        read_oc_config=lambda bot_id: (oc_cfg, oc_err),
        config_dir_probe=lambda path: config_dir_populated,
        probe_oc_channel=lambda bot_id, number: probe,
    )


# ── E.164 validation ─────────────────────────────────────────────────────────


class TestE164Validation:

    def test_valid_e164_passes(self):
        assert si.is_valid_e164("+15551234567") is True
        assert si.is_valid_e164("+447911123456") is True  # UK
        assert si.is_valid_e164("+8612345678901") is True  # CN-len

    def test_missing_plus_rejected(self):
        assert si.is_valid_e164("15551234567") is False

    def test_too_short_rejected(self):
        # 6 digits — Signal accepts >=7
        assert si.is_valid_e164("+123456") is False

    def test_too_long_rejected(self):
        # 16 digits — Signal accepts <=15
        assert si.is_valid_e164("+1234567890123456") is False

    def test_letters_rejected(self):
        assert si.is_valid_e164("+1555ABC4567") is False

    def test_empty_rejected(self):
        assert si.is_valid_e164("") is False
        assert si.is_valid_e164(None) is False


# ── State machine transitions ─────────────────────────────────────────────────


class TestResolveStatus:

    def test_fully_wired_returns_active(self):
        status = _resolve()
        assert status.status == "active"
        assert status.oc_probe_ok is True
        assert status.paired_number == _NUMBER
        assert status.plugin_version == "2026.6.1"
        assert status.device_name == "evolve-team-bot-a"

    def test_no_plugin_install_returns_plugin_not_installed(self):
        cfg = {"plugins": {"installs": {}, "entries": {}}, "channels": {}}
        status = _resolve(oc_cfg=cfg)
        assert status.status == "plugin_not_installed"

    def test_no_accounts_returns_number_not_captured(self):
        cfg = {
            "plugins": {
                "installs": {si.SIGNAL_PLUGIN_NPM: {"version": "2026.6.1"}},
                "entries": {"signal": {"enabled": False}},
            },
            "channels": {"signal": {"accounts": {}}},
        }
        status = _resolve(oc_cfg=cfg)
        assert status.status == "number_not_captured"
        assert status.plugin_version == "2026.6.1"

    def test_account_block_without_config_dir_returns_account_not_paired(self):
        cfg = {
            "plugins": {
                "installs": {si.SIGNAL_PLUGIN_NPM: {"version": "2026.6.1"}},
                "entries": {"signal": {"enabled": True}},
            },
            "channels": {
                "signal": {
                    "accounts": {
                        _NUMBER: {
                            "enabled": False,  # placeholder
                            "number": _NUMBER,
                        },
                    },
                },
            },
        }
        status = _resolve(oc_cfg=cfg)
        assert status.status == "account_not_paired"
        assert status.paired_number == _NUMBER

    def test_config_dir_unpopulated_returns_corrupt(self):
        status = _resolve(config_dir_populated=False)
        assert status.status == "config_dir_corrupt"

    def test_account_disabled_returns_disabled(self):
        cfg = {
            "plugins": {
                "installs": {si.SIGNAL_PLUGIN_NPM: {"version": "2026.6.1"}},
                "entries": {"signal": {"enabled": True}},
            },
            "channels": {
                "signal": {
                    "accounts": {
                        _NUMBER: {
                            "enabled": False,
                            "number": _NUMBER,
                            "configDir": "/x",
                        },
                    },
                },
            },
        }
        status = _resolve(oc_cfg=cfg)
        # _pick_primary_account falls back to first-present when none enabled
        assert status.status == "disabled"

    def test_probe_says_not_connected_returns_oc_probe_failed(self):
        """Load-bearing F3 rule."""
        status = _resolve(probe={
            "connected": False,
            "error": "phone_offline",
            "detail": "Linked-device session expired",
        })
        assert status.status == "oc_probe_failed"
        assert status.oc_probe_ok is False
        assert "expired" in (status.oc_probe_detail or "")

    def test_oc_config_read_failure_returns_unknown(self):
        status = _resolve(oc_cfg=None, oc_err="ENOENT: openclaw.json missing")
        assert status.status == "unknown"
        assert "ENOENT" in (status.error or "")

    def test_clawhub_spec_install_record_recognised(self):
        cfg = dict(_OK_OC_CFG)
        cfg["plugins"] = {
            "installs": {si.SIGNAL_CLAWHUB_SPEC: {"version": "2026.6.1"}},
            "entries": {"signal": {"enabled": True}},
        }
        status = _resolve(oc_cfg=cfg)
        assert status.status == "active"


# ── Install plan ──────────────────────────────────────────────────────────────


class TestBuildInstallPlan:

    def _status(self, **kwargs):
        return si.InstallStatus(bot_id="team-bot-a", **kwargs)

    def test_active_returns_empty_plan(self):
        plan = si.build_install_plan(self._status(status="active"))
        assert plan == []

    def test_unknown_returns_empty_plan(self):
        plan = si.build_install_plan(self._status(status="unknown", error="x"))
        assert plan == []

    def test_plugin_not_installed_includes_install_then_number_then_pair(self):
        plan = si.build_install_plan(self._status(status="plugin_not_installed"))
        ids = [s.id for s in plan]
        assert ids == ["install_plugin", "set_number", "pair_qr"]
        assert plan[0].access_panel is not None

    def test_number_not_captured_includes_set_number_then_pair(self):
        plan = si.build_install_plan(self._status(status="number_not_captured"))
        ids = [s.id for s in plan]
        assert ids == ["set_number", "pair_qr"]

    def test_account_not_paired_offers_only_pair(self):
        plan = si.build_install_plan(self._status(status="account_not_paired"))
        ids = [s.id for s in plan]
        assert ids == ["pair_qr"]

    def test_config_dir_corrupt_routes_to_repair(self):
        plan = si.build_install_plan(self._status(status="config_dir_corrupt"))
        ids = [s.id for s in plan]
        assert ids == ["pair_qr"]

    def test_disabled_offers_reenable_step(self):
        plan = si.build_install_plan(self._status(status="disabled"))
        ids = [s.id for s in plan]
        assert ids == ["reenable"]

    def test_oc_probe_failed_offers_reprobe(self):
        plan = si.build_install_plan(self._status(status="oc_probe_failed"))
        ids = [s.id for s in plan]
        assert ids == ["reprobe"]


# ── Capture number ────────────────────────────────────────────────────────────


class TestCaptureNumber:

    def test_valid_e164_writes_placeholder(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(si._oc_common, "read_oc_config",
                            lambda bot_id: ({}, None))
        def _w(bot_id, cfg):
            captured["cfg"] = cfg
            return True, None
        monkeypatch.setattr(si._oc_common, "write_oc_config", _w)
        monkeypatch.setattr(
            "evolve_admin.config.bot_home",
            lambda bot_id: Path("/Users/bot-user-a"),
        )

        ok, err = si.capture_number("team-bot-a", _NUMBER)
        assert ok and err is None

        accounts = captured["cfg"]["channels"]["signal"]["accounts"]
        assert _NUMBER in accounts
        # Placeholder: enabled=False until pair_qr completes
        assert accounts[_NUMBER]["enabled"] is False
        assert accounts[_NUMBER]["number"] == _NUMBER
        assert accounts[_NUMBER]["configDir"]
        assert accounts[_NUMBER]["deviceName"].startswith("evolve-")

    def test_invalid_e164_rejected_before_disk_write(self, monkeypatch):
        write_called = {"v": False}
        monkeypatch.setattr(si._oc_common, "read_oc_config",
                            lambda bot_id: ({}, None))
        monkeypatch.setattr(si._oc_common, "write_oc_config",
                            lambda bot_id, cfg: write_called.__setitem__("v", True))
        ok, err = si.capture_number("team-bot-a", "5551234567")  # missing +
        assert ok is False
        assert err == "number_invalid_e164"
        assert write_called["v"] is False


# ── enable_account_in_oc_config ──────────────────────────────────────────────


class TestEnableAccountInOcConfig:

    def test_fresh_write_lands_full_block(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(si._oc_common, "read_oc_config",
                            lambda bot_id: ({}, None))
        def _w(bot_id, cfg):
            captured["cfg"] = cfg
            return True, None
        monkeypatch.setattr(si._oc_common, "write_oc_config", _w)

        ok, err = si.enable_account_in_oc_config(
            "team-bot-a",
            number=_NUMBER,
            config_dir="/x/signal/config/15551234567",
            device_name="custom-device",
        )
        assert ok and err is None

        cfg = captured["cfg"]
        assert cfg["channels"]["signal"]["enabled"] is True
        assert cfg["channels"]["signal"]["dmPolicy"] == "pairing"
        acct = cfg["channels"]["signal"]["accounts"][_NUMBER]
        assert acct["enabled"] is True
        assert acct["number"] == _NUMBER
        assert acct["configDir"] == "/x/signal/config/15551234567"
        assert acct["deviceName"] == "custom-device"
        assert cfg["plugins"]["entries"]["signal"]["enabled"] is True

    def test_existing_channel_fields_preserved(self, monkeypatch):
        existing = {
            "channels": {
                "signal": {
                    "enabled": False,
                    "dmPolicy": "approve_only",  # operator-set
                    "accounts": {
                        _NUMBER: {
                            "enabled": False,
                            "number": _NUMBER,
                            "configDir": "/old",
                            "pluginHooks": {"messageReceived": True},  # operator-set
                        },
                    },
                },
            },
            "plugins": {"entries": {"signal": {"enabled": False}}},
        }
        captured = {}
        monkeypatch.setattr(si._oc_common, "read_oc_config",
                            lambda bot_id: (existing, None))
        def _w(bot_id, cfg):
            captured["cfg"] = cfg
            return True, None
        monkeypatch.setattr(si._oc_common, "write_oc_config", _w)

        ok, _ = si.enable_account_in_oc_config(
            "team-bot-a", number=_NUMBER, config_dir="/new",
        )
        assert ok

        sg = captured["cfg"]["channels"]["signal"]
        assert sg["enabled"] is True
        assert sg["dmPolicy"] == "approve_only"  # preserved
        acct = sg["accounts"][_NUMBER]
        assert acct["enabled"] is True
        assert acct["configDir"] == "/new"
        assert acct["pluginHooks"] == {"messageReceived": True}  # preserved

    def test_invalid_e164_fails_without_writing(self, monkeypatch):
        write_called = {"v": False}
        monkeypatch.setattr(si._oc_common, "read_oc_config",
                            lambda bot_id: ({}, None))
        monkeypatch.setattr(si._oc_common, "write_oc_config",
                            lambda bot_id, cfg: write_called.__setitem__("v", True))
        ok, err = si.enable_account_in_oc_config(
            "team-bot-a", number="5551234567",  # missing +
        )
        assert ok is False
        assert err == "number_invalid_e164"
        assert write_called["v"] is False

    def test_read_failure_propagates(self, monkeypatch):
        monkeypatch.setattr(si._oc_common, "read_oc_config",
                            lambda bot_id: (None, "ENOENT"))
        ok, err = si.enable_account_in_oc_config(
            "team-bot-a", number=_NUMBER, config_dir="/x",
        )
        assert ok is False
        assert "ENOENT" in (err or "")


# ── Revoke ───────────────────────────────────────────────────────────────────


class TestRevokeAccount:

    def test_single_account_revoke_removes_channel_and_plugin(self, monkeypatch):
        """Last-account revoke removes channels.signal + plugins.entries.signal
        entirely (parallel to whatsapp 2026-06-04 soft-disable bug fix)."""
        existing = {
            "channels": {
                "signal": {
                    "enabled": True,
                    "accounts": {
                        _NUMBER: {
                            "enabled": True, "number": _NUMBER,
                            "configDir": "/x",
                        },
                    },
                },
            },
            "plugins": {
                "entries": {"signal": {"enabled": True}},
                "installs": {si.SIGNAL_PLUGIN_NPM: {"version": "1.0.0"}},
            },
        }
        captured = {}
        monkeypatch.setattr(si._oc_common, "read_oc_config",
                            lambda bot_id: (existing, None))
        def _w(bot_id, cfg):
            captured["cfg"] = cfg
            return True, None
        monkeypatch.setattr(si._oc_common, "write_oc_config", _w)
        monkeypatch.setattr(si._oc_common, "kickstart_gateway",
                            lambda bot_id: (True, None))
        monkeypatch.setattr(si, "_bot_user_for", lambda bot_id: "bot-user-a")
        monkeypatch.setattr(
            "evolve_admin.config.bot_home",
            lambda bot_id: Path("/Users/bot-user-a"),
        )
        import subprocess as _sp
        monkeypatch.setattr(_sp, "run", lambda *a, **kw: None)

        ok, err = si.revoke_account("team-bot-a", number=_NUMBER)
        assert ok and err is None
        cfg = captured["cfg"]
        # Channel block fully removed (no enabled:false residue to nag operator about)
        assert "signal" not in cfg["channels"]
        assert "signal" not in cfg["plugins"]["entries"]
        assert si.SIGNAL_PLUGIN_NPM not in cfg["plugins"]["installs"]

    def test_multi_account_revoke_only_clears_target_account(self, monkeypatch):
        other = "+447911123456"
        existing = {
            "channels": {
                "signal": {
                    "enabled": True,
                    "accounts": {
                        _NUMBER: {
                            "enabled": True, "number": _NUMBER,
                            "configDir": "/x",
                        },
                        other: {
                            "enabled": True, "number": other,
                            "configDir": "/y",
                        },
                    },
                },
            },
            "plugins": {"entries": {"signal": {"enabled": True}}},
        }
        captured = {}
        monkeypatch.setattr(si._oc_common, "read_oc_config",
                            lambda bot_id: (existing, None))
        def _w(bot_id, cfg):
            captured["cfg"] = cfg
            return True, None
        monkeypatch.setattr(si._oc_common, "write_oc_config", _w)
        monkeypatch.setattr(si._oc_common, "kickstart_gateway",
                            lambda bot_id: (True, None))
        monkeypatch.setattr(si, "_bot_user_for", lambda bot_id: "bot-user-a")
        monkeypatch.setattr(
            "evolve_admin.config.bot_home",
            lambda bot_id: Path("/Users/bot-user-a"),
        )
        import subprocess as _sp
        monkeypatch.setattr(_sp, "run", lambda *a, **kw: None)

        ok, err = si.revoke_account("team-bot-a", number=_NUMBER)
        assert ok and err is None
        sg = captured["cfg"]["channels"]["signal"]
        # Target account popped; the other survives
        assert _NUMBER not in sg["accounts"]
        assert other in sg["accounts"]
        assert sg["enabled"] is True
        assert captured["cfg"]["plugins"]["entries"]["signal"]["enabled"] is True

    def test_invalid_e164_rejected(self):
        ok, err = si.revoke_account("team-bot-a", number="5551234567")
        assert ok is False
        assert err == "number_invalid_e164"


# ── Access panel ──────────────────────────────────────────────────────────────


class TestAccessPanelHonest:
    """The May audit's F5 rule + the Plex test."""

    def test_no_jargon_in_user_facing_strings(self):
        forbidden = (
            "signal-cli", "libsignal", "AGPL", "GPL", "Baileys", "configDir",
            "authDir", "TCC", "AppleScript", "sudoers",
        )
        panel_text = " ".join([
            si.SIGNAL_ACCESS_PANEL["summary"],
            *si.SIGNAL_ACCESS_PANEL["will"],
            *si.SIGNAL_ACCESS_PANEL["wont"],
            si.SIGNAL_ACCESS_PANEL["where_credentials_live"],
        ])
        hits = [w for w in forbidden if w.lower() in panel_text.lower()]
        assert hits == [], f"Jargon found in access panel: {hits}"

    def test_recommends_separate_phone_number(self):
        summary = si.SIGNAL_ACCESS_PANEL["summary"].lower()
        assert "separate phone" in summary or "spare" in summary

    def test_where_credentials_live_uses_signal_app_terminology(self):
        cred = si.SIGNAL_ACCESS_PANEL["where_credentials_live"].lower()
        # Matches the wording Signal's mobile app uses
        assert "linked devices" in cred

    def test_wont_lists_share_account_concern(self):
        wont = " ".join(si.SIGNAL_ACCESS_PANEL["wont"]).lower()
        assert "share" in wont


# ── Skill registry entry ──────────────────────────────────────────────────────


class TestSkillRegistryEntry:

    def test_has_required_fields(self):
        entry = si.SKILL_REGISTRY_ENTRY
        assert entry["id"] == si.SIGNAL_SKILL_ID
        assert entry["display_name"] == "Signal"
        assert "access_panel" in entry
        assert "summary" in entry

    def test_config_keys_describe_account_shape(self):
        keys = si.SKILL_REGISTRY_ENTRY["config_keys"]
        # Number-keyed accounts shape — UI uses these to render inventory tile
        assert any("number" in k for k in keys)
        assert any("configDir" in k for k in keys)
