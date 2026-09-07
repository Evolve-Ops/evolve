"""Tests for evolve_admin.skills.whatsapp_install.

Coverage:
- TestResolveStatus      — every state-machine branch + load-bearing F3 rule
- TestBuildInstallPlan   — each state maps to the right ordered step list
- TestEnableAccountInOcConfig — write semantics + operator-field preservation
- TestRevokeAccount      — symmetric revoke + multi-account behaviour
- TestAccessPanelHonest  — Plex-test + F5 no-aspirational-promises rule
- TestSkillRegistryEntry — UI hooks (catalog detail dispatcher reads these)

All OC config reads/writes + CLI probes are stubbed via injectable callables.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.skills import whatsapp_install as wi  # noqa: E402


# ── Test helpers ──────────────────────────────────────────────────────────────

# Canonical "fully wired" openclaw.json shape — every test that wants a
# happy-path resolution starts from this and removes/mutates fields.
_ACCT_ID = wi.DEFAULT_ACCOUNT_ID
_OK_OC_CFG = {
    "plugins": {
        "installs": {wi.WHATSAPP_PLUGIN_NPM: {"version": "2026.6.1"}},
        "entries": {"whatsapp": {"enabled": True}},
    },
    "channels": {
        "whatsapp": {
            "enabled": True,
            "accounts": {
                _ACCT_ID: {
                    "enabled": True,
                    "authDir": "/Users/bot-user-a/.openclaw/whatsapp/auth",
                    "name": "team-bot-a-primary",
                },
            },
        },
    },
}


def _resolve(
    *,
    oc_cfg: dict | None = None,
    oc_err: str | None = None,
    auth_dir_populated: bool = True,
    probe: dict | None = None,
):
    """Wrap resolve_status with injected callables — no disk I/O."""
    if oc_cfg is None and oc_err is None:
        oc_cfg = _OK_OC_CFG
    if probe is None:
        probe = {"connected": True, "paired_phone": "+15551234567"}
    return wi.resolve_status(
        "team-bot-a",
        read_oc_config=lambda bot_id: (oc_cfg, oc_err),
        auth_dir_probe=lambda path: auth_dir_populated,
        probe_oc_channel=lambda bot_id: probe,
    )


# ── State machine transitions ─────────────────────────────────────────────────


class TestResolveStatus:

    def test_fully_wired_returns_active(self):
        status = _resolve()
        assert status.status == "active"
        assert status.oc_probe_ok is True
        assert status.paired_phone == "+15551234567"
        assert status.account_id == _ACCT_ID
        assert status.plugin_version == "2026.6.1"

    def test_no_plugin_install_record_returns_plugin_not_installed(self):
        cfg = {"plugins": {"installs": {}, "entries": {}}, "channels": {}}
        status = _resolve(oc_cfg=cfg)
        assert status.status == "plugin_not_installed"

    def test_plugin_installed_but_no_accounts_returns_account_not_paired(self):
        cfg = {
            "plugins": {
                "installs": {wi.WHATSAPP_PLUGIN_NPM: {"version": "2026.6.1"}},
                "entries": {"whatsapp": {"enabled": False}},
            },
            "channels": {"whatsapp": {"accounts": {}}},
        }
        status = _resolve(oc_cfg=cfg)
        assert status.status == "account_not_paired"
        assert status.plugin_version == "2026.6.1"

    def test_account_block_missing_authdir_returns_account_not_paired(self):
        cfg = {
            "plugins": {
                "installs": {wi.WHATSAPP_PLUGIN_NPM: {"version": "2026.6.1"}},
                "entries": {"whatsapp": {"enabled": True}},
            },
            "channels": {
                "whatsapp": {
                    "accounts": {_ACCT_ID: {"enabled": True}},  # no authDir
                },
            },
        }
        status = _resolve(oc_cfg=cfg)
        assert status.status == "account_not_paired"

    def test_auth_dir_present_but_unpopulated_returns_auth_dir_corrupt(self):
        status = _resolve(auth_dir_populated=False)
        assert status.status == "auth_dir_corrupt"
        assert status.auth_dir.endswith("/whatsapp/auth")

    def test_account_disabled_returns_disabled(self):
        cfg = {
            "plugins": {
                "installs": {wi.WHATSAPP_PLUGIN_NPM: {"version": "2026.6.1"}},
                "entries": {"whatsapp": {"enabled": True}},
            },
            "channels": {
                "whatsapp": {
                    "accounts": {
                        _ACCT_ID: {
                            "enabled": False,  # operator turned this account off
                            "authDir": "/x",
                        },
                    },
                },
            },
        }
        status = _resolve(oc_cfg=cfg)
        assert status.status == "disabled"

    def test_channel_disabled_returns_disabled(self):
        cfg = {
            "plugins": {
                "installs": {wi.WHATSAPP_PLUGIN_NPM: {"version": "2026.6.1"}},
                "entries": {"whatsapp": {"enabled": True}},
            },
            "channels": {
                "whatsapp": {
                    "enabled": False,  # channel turned off
                    "accounts": {
                        _ACCT_ID: {"enabled": True, "authDir": "/x"},
                    },
                },
            },
        }
        status = _resolve(oc_cfg=cfg)
        assert status.status == "disabled"

    def test_probe_says_not_connected_returns_oc_probe_failed(self):
        """Load-bearing F3 rule: never return ``active`` from config
        presence alone. Probe is the load-bearing check."""
        status = _resolve(probe={
            "connected": False,
            "error": "phone_offline",
            "detail": "Linked-device session expired",
        })
        assert status.status == "oc_probe_failed"
        assert status.oc_probe_ok is False
        assert "expired" in (status.oc_probe_detail or "")

    def test_probe_inconclusive_treated_as_failed(self):
        status = _resolve(probe={"connected": False, "error": "probe_inconclusive"})
        assert status.status == "oc_probe_failed"

    def test_oc_config_read_failure_returns_unknown(self):
        status = _resolve(oc_cfg=None, oc_err="ENOENT: openclaw.json missing")
        assert status.status == "unknown"
        assert "ENOENT" in (status.error or "")

    def test_clawhub_spec_install_record_recognised(self):
        """The plugin install record key can be the npm name OR the
        clawhub spec — both should resolve to plugin-installed."""
        cfg = dict(_OK_OC_CFG)
        cfg["plugins"] = {
            "installs": {wi.WHATSAPP_CLAWHUB_SPEC: {"version": "2026.6.1"}},
            "entries": {"whatsapp": {"enabled": True}},
        }
        status = _resolve(oc_cfg=cfg)
        assert status.status == "active"


# ── Install plan ──────────────────────────────────────────────────────────────


class TestBuildInstallPlan:

    def _status(self, **kwargs):
        return wi.InstallStatus(bot_id="team-bot-a", **kwargs)

    def test_active_returns_empty_plan(self):
        plan = wi.build_install_plan(self._status(status="active"))
        assert plan == []

    def test_unknown_returns_empty_plan(self):
        plan = wi.build_install_plan(self._status(status="unknown", error="x"))
        assert plan == []

    def test_plugin_not_installed_includes_install_then_pair(self):
        plan = wi.build_install_plan(self._status(status="plugin_not_installed"))
        ids = [s.id for s in plan]
        assert ids == ["install_plugin", "pair_qr"]
        # Access panel attached to the first step
        assert plan[0].access_panel is not None

    def test_account_not_paired_includes_only_pair_step(self):
        plan = wi.build_install_plan(self._status(status="account_not_paired"))
        ids = [s.id for s in plan]
        assert ids == ["pair_qr"]

    def test_auth_dir_corrupt_routes_to_repair(self):
        plan = wi.build_install_plan(self._status(status="auth_dir_corrupt"))
        ids = [s.id for s in plan]
        assert ids == ["pair_qr"]  # same QR pairing flow re-runs

    def test_disabled_offers_reenable_step(self):
        plan = wi.build_install_plan(self._status(status="disabled"))
        ids = [s.id for s in plan]
        assert ids == ["reenable"]

    def test_oc_probe_failed_offers_reprobe(self):
        plan = wi.build_install_plan(self._status(status="oc_probe_failed"))
        ids = [s.id for s in plan]
        assert ids == ["reprobe"]


# ── OC config writes ──────────────────────────────────────────────────────────


class TestEnableAccountInOcConfig:

    def test_fresh_write_lands_full_block(self, monkeypatch):
        captured = {}

        monkeypatch.setattr(wi._oc_common, "read_oc_config",
                            lambda bot_id: ({}, None))
        def _w(bot_id, cfg):
            captured["cfg"] = cfg
            return True, None
        monkeypatch.setattr(wi._oc_common, "write_oc_config", _w)
        monkeypatch.setattr(
            "evolve_admin.config.bot_home",
            lambda bot_id: Path("/Users/bot-user-a"),
        )

        ok, err = wi.enable_account_in_oc_config(
            "team-bot-a",
            auth_dir="/Users/bot-user-a/.openclaw/whatsapp/auth",
            paired_phone="+15551234567",
        )
        assert ok and err is None

        cfg = captured["cfg"]
        assert cfg["channels"]["whatsapp"]["enabled"] is True
        assert cfg["channels"]["whatsapp"]["dmPolicy"] == "pairing"
        acct = cfg["channels"]["whatsapp"]["accounts"][wi.DEFAULT_ACCOUNT_ID]
        assert acct["enabled"] is True
        assert acct["authDir"].endswith("whatsapp/auth")
        assert acct["phoneNumber"] == "+15551234567"
        assert cfg["plugins"]["entries"]["whatsapp"]["enabled"] is True

    def test_existing_operator_set_fields_preserved(self, monkeypatch):
        """Channel-level operator-set fields (e.g. dmPolicy) survive a
        re-pair. Only enabled + authDir + name get rewritten."""
        existing = {
            "channels": {
                "whatsapp": {
                    "enabled": False,
                    "dmPolicy": "approve_only",  # operator-set
                    "mediaMaxMb": 100,           # operator-set
                    "accounts": {
                        wi.DEFAULT_ACCOUNT_ID: {
                            "enabled": False,
                            "authDir": "/old/auth",
                            # operator-set per-account knob
                            "pluginHooks": {"messageReceived": True},
                        },
                    },
                },
            },
            "plugins": {"entries": {"whatsapp": {"enabled": False}}},
        }
        captured = {}
        monkeypatch.setattr(wi._oc_common, "read_oc_config",
                            lambda bot_id: (existing, None))
        def _w(bot_id, cfg):
            captured["cfg"] = cfg
            return True, None
        monkeypatch.setattr(wi._oc_common, "write_oc_config", _w)

        ok, _ = wi.enable_account_in_oc_config(
            "team-bot-a", auth_dir="/new/auth",
        )
        assert ok

        cfg = captured["cfg"]
        wa = cfg["channels"]["whatsapp"]
        # Install flow overwrites enabled + authDir
        assert wa["enabled"] is True
        # Channel-level operator-set fields preserved
        assert wa["dmPolicy"] == "approve_only"
        assert wa["mediaMaxMb"] == 100
        acct = wa["accounts"][wi.DEFAULT_ACCOUNT_ID]
        assert acct["enabled"] is True
        assert acct["authDir"] == "/new/auth"
        # Per-account operator-set fields preserved
        assert acct["pluginHooks"] == {"messageReceived": True}

    def test_read_failure_propagates_without_writing(self, monkeypatch):
        monkeypatch.setattr(wi._oc_common, "read_oc_config",
                            lambda bot_id: (None, "ENOENT"))
        write_called = {"v": False}
        monkeypatch.setattr(wi._oc_common, "write_oc_config",
                            lambda bot_id, cfg: write_called.__setitem__("v", True))
        ok, err = wi.enable_account_in_oc_config("team-bot-a", auth_dir="/x")
        assert ok is False
        assert "ENOENT" in (err or "")
        assert write_called["v"] is False


# ── Revoke ─────────────────────────────────────────────────────────────────────


class TestRevokeAccount:

    def test_single_account_revoke_removes_channel_and_plugin(self, monkeypatch):
        """Last-account revoke wipes channels.whatsapp + plugins.entries.whatsapp
        + plugins.installs[*whatsapp*] entirely — does NOT leave an
        ``enabled: false`` orphan (which the old behaviour did and the
        Skills page then nagged the operator to "complete the install" —
        the documented 2026-06-04 legacy-orphan bug)."""
        existing = {
            "channels": {
                "whatsapp": {
                    "enabled": True,
                    "accounts": {
                        wi.DEFAULT_ACCOUNT_ID: {
                            "enabled": True, "authDir": "/x",
                        },
                    },
                },
            },
            "plugins": {
                "entries": {"whatsapp": {"enabled": True}},
                "installs": {wi.WHATSAPP_PLUGIN_NPM: {"version": "1.0.0"}},
            },
        }
        captured = {}
        monkeypatch.setattr(wi._oc_common, "read_oc_config",
                            lambda bot_id: (existing, None))
        def _w(bot_id, cfg):
            captured["cfg"] = cfg
            return True, None
        monkeypatch.setattr(wi._oc_common, "write_oc_config", _w)
        monkeypatch.setattr(wi._oc_common, "kickstart_gateway",
                            lambda bot_id: (True, None))
        monkeypatch.setattr(wi, "_bot_user_for", lambda bot_id: "bot-user-a")
        monkeypatch.setattr(
            "evolve_admin.config.bot_home",
            lambda bot_id: Path("/Users/bot-user-a"),
        )
        # Avoid invoking the real openclaw CLI during the logout step
        import subprocess as _sp
        monkeypatch.setattr(_sp, "run", lambda *a, **kw: None)

        ok, err = wi.revoke_account("team-bot-a")
        assert ok and err is None
        cfg = captured["cfg"]
        # Channel block fully removed (no enabled:false residue)
        assert "whatsapp" not in cfg["channels"]
        # Plugin entry gone too
        assert "whatsapp" not in cfg["plugins"]["entries"]
        # Plugin install record cleared so a re-install isn't skipped
        assert wi.WHATSAPP_PLUGIN_NPM not in cfg["plugins"]["installs"]

    def test_legacy_orphan_shape_cleared(self, monkeypatch):
        """The documented 2026-06-04 legacy-orphan shape:
        ``channels.whatsapp = {enabled: False, dmPolicy, groupPolicy,
        debounceMs, mediaMaxMb}`` with no ``accounts`` sub-key and no
        ``plugins.entries.whatsapp``. The legacy uninstall left this
        orphan behind; the new revoke removes it cleanly."""
        existing = {
            "channels": {
                "whatsapp": {
                    "enabled": False,
                    "dmPolicy": "pairing",
                    "groupPolicy": "allowlist",
                    "debounceMs": 0,
                    "mediaMaxMb": 50,
                },
                "slack": {  # unrelated channel — must survive
                    "enabled": True, "botToken": "xoxb-keep-me",
                },
            },
            "plugins": {"entries": {"slack": {"enabled": True}}},
        }
        captured = {}
        monkeypatch.setattr(wi._oc_common, "read_oc_config",
                            lambda bot_id: (existing, None))
        def _w(bot_id, cfg):
            captured["cfg"] = cfg
            return True, None
        monkeypatch.setattr(wi._oc_common, "write_oc_config", _w)
        monkeypatch.setattr(wi._oc_common, "kickstart_gateway",
                            lambda bot_id: (True, None))
        monkeypatch.setattr(wi, "_bot_user_for", lambda bot_id: "team-bot-user-a")
        monkeypatch.setattr(
            "evolve_admin.config.bot_home",
            lambda bot_id: Path("/Users/team-bot-user-a"),
        )
        import subprocess as _sp
        monkeypatch.setattr(_sp, "run", lambda *a, **kw: None)

        ok, err = wi.revoke_account("team-bot-a")
        assert ok and err is None
        cfg = captured["cfg"]
        # The whatsapp orphan is gone
        assert "whatsapp" not in cfg["channels"]
        # Slack survives untouched — the helper only touches the named channel
        assert cfg["channels"]["slack"]["botToken"] == "xoxb-keep-me"
        assert cfg["plugins"]["entries"]["slack"]["enabled"] is True

    def test_multi_account_revoke_only_clears_target_account(self, monkeypatch):
        """When other accounts remain, the channel block stays (just minus
        the target account). plugins.entries.whatsapp stays enabled."""
        existing = {
            "channels": {
                "whatsapp": {
                    "enabled": True,
                    "accounts": {
                        wi.DEFAULT_ACCOUNT_ID: {
                            "enabled": True, "authDir": "/x",
                        },
                        "secondary": {
                            "enabled": True, "authDir": "/y",
                        },
                    },
                },
            },
            "plugins": {"entries": {"whatsapp": {"enabled": True}}},
        }
        captured = {}
        monkeypatch.setattr(wi._oc_common, "read_oc_config",
                            lambda bot_id: (existing, None))
        def _w(bot_id, cfg):
            captured["cfg"] = cfg
            return True, None
        monkeypatch.setattr(wi._oc_common, "write_oc_config", _w)
        monkeypatch.setattr(wi._oc_common, "kickstart_gateway",
                            lambda bot_id: (True, None))
        monkeypatch.setattr(wi, "_bot_user_for", lambda bot_id: "bot-user-a")
        monkeypatch.setattr(
            "evolve_admin.config.bot_home",
            lambda bot_id: Path("/Users/bot-user-a"),
        )
        import subprocess as _sp
        monkeypatch.setattr(_sp, "run", lambda *a, **kw: None)

        ok, err = wi.revoke_account("team-bot-a")
        assert ok and err is None
        wa = captured["cfg"]["channels"]["whatsapp"]
        # Target account popped; the other survives
        assert wi.DEFAULT_ACCOUNT_ID not in wa["accounts"]
        assert "secondary" in wa["accounts"]
        assert wa["enabled"] is True
        assert captured["cfg"]["plugins"]["entries"]["whatsapp"]["enabled"] is True

    def test_revoke_is_idempotent_against_empty_state(self, monkeypatch):
        """Operator clicking Uninstall twice should not error."""
        existing = {"channels": {}, "plugins": {"entries": {}}}
        captured = {"writes": 0}
        monkeypatch.setattr(wi._oc_common, "read_oc_config",
                            lambda bot_id: (existing, None))
        def _w(bot_id, cfg):
            captured["writes"] += 1
            return True, None
        monkeypatch.setattr(wi._oc_common, "write_oc_config", _w)
        monkeypatch.setattr(wi._oc_common, "kickstart_gateway",
                            lambda bot_id: (True, None))
        monkeypatch.setattr(wi, "_bot_user_for", lambda bot_id: "team-bot-user-a")
        monkeypatch.setattr(
            "evolve_admin.config.bot_home",
            lambda bot_id: Path("/Users/team-bot-user-a"),
        )
        import subprocess as _sp
        monkeypatch.setattr(_sp, "run", lambda *a, **kw: None)

        ok, err = wi.revoke_account("team-bot-a")
        assert ok and err is None


class TestResolveStatusLegacyOrphan:
    """Documented 2026-06-04 scenario: ``channels.whatsapp = {enabled: false, …}``
    with no ``accounts`` sub-key and no plugin install record. The pre-fix
    resolve_status returned ``plugin_not_installed`` which made the Skills
    page render '+ Add to <bot>' — the persistent nag. The fix surfaces a
    ``legacy_orphan`` state so the page can show the Uninstall affordance
    instead."""

    def test_bare_channel_block_without_plugin_record_returns_legacy_orphan(self, monkeypatch):
        cfg = {
            "channels": {
                "whatsapp": {
                    "enabled": False,
                    "dmPolicy": "pairing",
                    "groupPolicy": "allowlist",
                    "debounceMs": 0,
                    "mediaMaxMb": 50,
                },
            },
            "plugins": {"entries": {}, "installs": {}},
        }
        monkeypatch.setattr(wi._oc_common, "read_oc_config",
                            lambda bot_id: (cfg, None))
        st = wi.resolve_status("team-bot-a",
                               read_oc_config=lambda bot_id: (cfg, None),
                               auth_dir_probe=lambda p: False,
                               probe_oc_channel=lambda bot_id: {"connected": False})
        assert st.status == "legacy_orphan"

    def test_no_channel_block_still_returns_plugin_not_installed(self, monkeypatch):
        """A truly-never-installed bot still gets 'plugin_not_installed'
        (so the Skills page can show '+ Add')."""
        cfg = {"channels": {}, "plugins": {"entries": {}, "installs": {}}}
        st = wi.resolve_status("fresh-bot",
                               read_oc_config=lambda bot_id: (cfg, None),
                               auth_dir_probe=lambda p: False,
                               probe_oc_channel=lambda bot_id: {"connected": False})
        assert st.status == "plugin_not_installed"

    def test_channel_with_empty_accounts_still_legacy_orphan(self, monkeypatch):
        """``channels.whatsapp = {accounts: {}}`` is still an orphan —
        the operator started pairing, the account block was created
        empty, then they abandoned."""
        cfg = {
            "channels": {"whatsapp": {"enabled": False, "accounts": {}}},
            "plugins": {"entries": {}, "installs": {}},
        }
        st = wi.resolve_status("team-bot-a",
                               read_oc_config=lambda bot_id: (cfg, None),
                               auth_dir_probe=lambda p: False,
                               probe_oc_channel=lambda bot_id: {"connected": False})
        assert st.status == "legacy_orphan"


# ── Access panel honesty ─────────────────────────────────────────────────────


class TestAccessPanelHonest:
    """The May audit's F5 rule: every line in ``will`` must map to
    something the bundled @openclaw/whatsapp plugin actually delivers."""

    def test_no_jargon_in_user_facing_strings(self):
        forbidden = (
            "Baileys", "authDir", "creds.json", "ClawHub",
            "npm", "sudoers", "TCC", "AppleScript",
        )
        panel_text = " ".join([
            wi.WHATSAPP_ACCESS_PANEL["summary"],
            *wi.WHATSAPP_ACCESS_PANEL["will"],
            *wi.WHATSAPP_ACCESS_PANEL["wont"],
            wi.WHATSAPP_ACCESS_PANEL["where_credentials_live"],
        ])
        hits = [w for w in forbidden if w.lower() in panel_text.lower()]
        assert hits == [], f"Jargon found in access panel: {hits}"

    def test_recommends_separate_phone_number(self):
        """OC's own catalog blurb says 'recommend a separate phone +
        eSIM'. Plex-test honesty means we surface this in the summary,
        not bury it."""
        summary = wi.WHATSAPP_ACCESS_PANEL["summary"].lower()
        assert "separate phone" in summary or "spare" in summary

    def test_wont_lists_obvious_negatives(self):
        """The 'won't' list must include the share-account negative, which
        is the most common WhatsApp Web concern."""
        wont = " ".join(wi.WHATSAPP_ACCESS_PANEL["wont"]).lower()
        assert "share" in wont
        # Also names the linked-device concept the operator will see on
        # their phone — matches the wording WhatsApp's UI uses.
        cred = wi.WHATSAPP_ACCESS_PANEL["where_credentials_live"].lower()
        assert "linked devices" in cred


# ── Skill registry entry ──────────────────────────────────────────────────────


class TestSkillRegistryEntry:

    def test_has_required_fields(self):
        entry = wi.SKILL_REGISTRY_ENTRY
        assert entry["id"] == wi.WHATSAPP_SKILL_ID
        assert entry["display_name"] == "WhatsApp"
        assert "access_panel" in entry
        assert "summary" in entry

    def test_config_keys_describe_account_shape(self):
        keys = wi.SKILL_REGISTRY_ENTRY["config_keys"]
        # The account shape is what the UI needs to render an inventory
        # tile — authDir presence == configured.
        assert any("authDir" in k for k in keys)
