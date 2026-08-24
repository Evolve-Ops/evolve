"""tests/test_imessage_install.py — iMessage skill install state machine (V2.1-6 Task 3).

Tests for imessage_install.py:
  - resolve_status routes by (TCC grants, Messages.app state, config) into the state machine.
  - build_install_plan returns the correct ordered steps for each state.
  - All checks are injectable callables — no actual TCC or Messages.app calls.
  - Status values are correct for each blocking condition.
  - The access panel is present on the first blocking step.

The resolve_status function uses injected callables so it is fully testable
without macOS-specific permissions.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
for _path in (str(_ADMIN_DIR),):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from evolve_admin.skills import imessage_install as ii  # noqa: E402


# ── Test helpers ──────────────────────────────────────────────────────────────


# Sentinel — distinguishes "default-to-wired" from "explicitly empty".
# Tests asserting the new OC stages pass explicit ``None`` (or a partial
# dict) for oc_channel/oc_plugin_entry; tests that just want a fully-
# wired bot use the default sentinel.
_UNSET = object()


def _make_resolve(
    fda: bool = True,
    auto: bool = True,
    running: bool = True,
    signed_in: bool = True,
    handle: str | None = "me@icloud.com",
    config: dict | None = None,
    # 2026-06-04 bundled-plugin rewire — by default the OC stages stub to
    # "wired + connected" so the pre-rewire tests still assert ``active``
    # at the end of the state machine. Pass an explicit ``None`` to test
    # the not_wired_to_oc state; pass an explicit dict to test variants.
    oc_channel=_UNSET,
    oc_plugin_entry=_UNSET,
    oc_read_err: str | None = None,
    oc_probe=_UNSET,
) -> ii.InstallStatus:
    """Call resolve_status with injected callables."""
    if config is None and handle:
        config = {"handle": handle, "allowed_senders": [], "active_since": "2026-05-13T00:00:00+00:00"}
    elif config is None:
        config = {}

    # OC stage defaults: when handle is set AND the caller didn't pass an
    # explicit override, default to a fully-wired + connected OC state.
    # When handle is None, the resolver short-circuits at
    # handle_not_configured before reaching these.
    if oc_channel is _UNSET:
        oc_channel = ({"enabled": True, "handle": handle, "dbPath": "/x/chat.db",
                       "service": "auto"} if handle else None)
    if oc_plugin_entry is _UNSET:
        oc_plugin_entry = ({"enabled": True} if handle else None)
    if oc_probe is _UNSET:
        oc_probe = ({"connected": True} if handle else {"connected": False})

    return ii.resolve_status(
        "testbot",
        check_tcc_fda=lambda: fda,
        check_tcc_automation=lambda: auto,
        check_messages_running=lambda: running,
        check_signed_in=lambda: (signed_in, handle if signed_in else None),
        read_config=lambda bot_id: config,
        read_oc_block=lambda bot_id: (oc_channel, oc_plugin_entry, oc_read_err),
        probe_oc_channel=lambda bot_id: oc_probe,
    )


# ── State machine transitions ─────────────────────────────────────────────────


class TestResolveStatus:
    def test_fully_configured_returns_active(self):
        status = _make_resolve()
        assert status.status == "active"
        assert status.tcc_fda_granted is True
        assert status.tcc_automation_granted is True
        assert status.messages_app_running is True
        assert status.signed_in is True
        assert status.imessage_handle == "me@icloud.com"

    def test_missing_fda_returns_no_tcc_fda(self):
        status = _make_resolve(fda=False)
        assert status.status == "no_tcc_fda"
        assert status.tcc_fda_granted is False

    def test_missing_automation_returns_no_tcc_automation(self):
        status = _make_resolve(auto=False)
        assert status.status == "no_tcc_automation"
        assert status.tcc_automation_granted is False
        assert status.tcc_fda_granted is True  # FDA is OK

    def test_messages_not_running_returns_correct_status(self):
        status = _make_resolve(running=False)
        assert status.status == "messages_not_running"
        assert status.messages_app_running is False
        assert status.tcc_fda_granted is True
        assert status.tcc_automation_granted is True

    def test_not_signed_in_returns_not_signed_in(self):
        status = _make_resolve(signed_in=False)
        assert status.status == "not_signed_in"
        assert status.signed_in is False
        assert status.messages_app_running is True

    def test_no_handle_configured_returns_handle_not_configured(self):
        status = _make_resolve(handle=None, config={})
        assert status.status == "handle_not_configured"
        assert status.imessage_handle is None

    def test_blocking_order_fda_before_automation(self):
        """If both FDA and Automation are missing, FDA blocks first."""
        status = _make_resolve(fda=False, auto=False)
        assert status.status == "no_tcc_fda"

    def test_blocking_order_automation_before_running(self):
        status = _make_resolve(auto=False, running=False)
        assert status.status == "no_tcc_automation"

    def test_unknown_on_fda_check_exception(self):
        def _raise():
            raise RuntimeError("TCC check failed")

        status = ii.resolve_status(
            "testbot",
            check_tcc_fda=_raise,
            check_tcc_automation=lambda: True,
            check_messages_running=lambda: True,
            check_signed_in=lambda: (True, "me@icloud.com"),
            read_config=lambda _: {"handle": "me@icloud.com"},
        )
        assert status.status == "unknown"
        assert "fda_check_failed" in (status.error or "")

    def test_config_allowed_senders_preserved(self):
        status = _make_resolve(config={"handle": "me@icloud.com", "allowed_senders": ["+1555", "alice@example.com"]})
        assert status.status == "active"
        assert "+1555" in status.allowed_senders
        assert "alice@example.com" in status.allowed_senders

    def test_active_since_preserved(self):
        status = _make_resolve(config={"handle": "me@icloud.com", "active_since": "2026-05-01T12:00:00+00:00"})
        assert status.status == "active"
        assert status.active_since == "2026-05-01T12:00:00+00:00"


# ── to_dict shape ─────────────────────────────────────────────────────────────


class TestInstallStatusToDict:
    def test_to_dict_has_required_fields(self):
        status = _make_resolve()
        d = status.to_dict()
        assert d["skill_id"] == ii.IMESSAGE_SKILL_ID
        assert d["kind"] == ii.IMESSAGE_SKILL_KIND
        assert d["status"] == "active"
        assert "tcc_fda_granted" in d
        assert "tcc_automation_granted" in d
        assert "messages_app_running" in d
        assert "signed_in" in d
        assert "imessage_handle" in d
        assert "allowed_senders" in d

    def test_to_dict_bot_id(self):
        status = _make_resolve()
        assert status.to_dict()["bot_id"] == "testbot"


# ── build_install_plan ────────────────────────────────────────────────────────


class TestBuildInstallPlan:
    def test_active_returns_empty_plan(self):
        status = _make_resolve()
        plan = ii.build_install_plan(status)
        assert plan == []

    def test_no_tcc_fda_returns_grant_fda_step(self):
        status = _make_resolve(fda=False)
        plan = ii.build_install_plan(status)
        assert len(plan) >= 1
        assert plan[0].id == "grant_fda"

    def test_no_tcc_automation_returns_automation_step(self):
        status = _make_resolve(auto=False)
        plan = ii.build_install_plan(status)
        step_ids = [s.id for s in plan]
        assert "grant_automation" in step_ids

    def test_messages_not_running_returns_open_messages_step(self):
        status = _make_resolve(running=False)
        plan = ii.build_install_plan(status)
        step_ids = [s.id for s in plan]
        assert "open_messages" in step_ids

    def test_not_signed_in_returns_sign_in_step(self):
        status = _make_resolve(signed_in=False)
        plan = ii.build_install_plan(status)
        step_ids = [s.id for s in plan]
        assert "sign_in_imessage" in step_ids

    def test_handle_not_configured_returns_set_handle_step(self):
        status = _make_resolve(handle=None, config={})
        plan = ii.build_install_plan(status)
        step_ids = [s.id for s in plan]
        assert "set_handle" in step_ids

    def test_first_step_has_access_panel(self):
        """The first blocking step must include the access panel."""
        status = _make_resolve(fda=False)
        plan = ii.build_install_plan(status)
        assert plan[0].access_panel is not None
        assert "will" in plan[0].access_panel
        assert "wont" in plan[0].access_panel

    def test_steps_have_endpoint(self):
        """All steps must have an endpoint for the UI to call."""
        status = _make_resolve(fda=False)
        plan = ii.build_install_plan(status)
        for step in plan:
            assert step.endpoint is not None

    def test_steps_have_description(self):
        status = _make_resolve(fda=False)
        plan = ii.build_install_plan(status)
        for step in plan:
            assert step.description  # non-empty

    def test_to_dict_on_steps(self):
        status = _make_resolve(fda=False)
        plan = ii.build_install_plan(status)
        for step in plan:
            d = step.to_dict()
            assert "id" in d
            assert "label" in d
            assert "endpoint" in d
            assert "description" in d

    def test_all_steps_when_nothing_configured(self):
        """If nothing is configured, plan should have all 5 steps in order."""
        status = _make_resolve(fda=False, auto=False, running=False, signed_in=False, handle=None, config={})
        plan = ii.build_install_plan(status)
        step_ids = [s.id for s in plan]
        # fda is the first blocker; others accumulate
        assert "grant_fda" in step_ids
        assert len(plan) >= 2

    def test_settings_link_present_on_fda_step(self):
        """FDA step should have a settings_link pointing to System Settings."""
        status = _make_resolve(fda=False)
        plan = ii.build_install_plan(status)
        fda_step = next(s for s in plan if s.id == "grant_fda")
        assert fda_step.settings_link is not None
        assert "x-apple.systempreferences" in fda_step.settings_link

    def test_unknown_status_returns_empty_plan(self):
        status = ii.InstallStatus(bot_id="testbot", status="unknown", error="something broke")
        plan = ii.build_install_plan(status)
        assert plan == []


# ── Access panel validation ───────────────────────────────────────────────────


class TestAccessPanel:
    def test_access_panel_has_will_and_wont(self):
        assert "will" in ii.IMESSAGE_ACCESS_PANEL
        assert "wont" in ii.IMESSAGE_ACCESS_PANEL

    def test_access_panel_no_jargon(self):
        """Plex test: no TCC, AppleScript, SQLite, ROWID in user-facing text."""
        forbidden = ["TCC", "AppleScript", "ROWID", "sqlite"]
        will_text = " ".join(ii.IMESSAGE_ACCESS_PANEL.get("will", []))
        wont_text = " ".join(ii.IMESSAGE_ACCESS_PANEL.get("wont", []))
        combined = will_text + " " + wont_text
        for term in forbidden:
            assert term.lower() not in combined.lower(), f"Jargon found: {term!r}"

    def test_tcc_permissions_documented(self):
        """Access panel must list the TCC permissions required."""
        perms = ii.IMESSAGE_ACCESS_PANEL.get("tcc_permissions_required", [])
        assert len(perms) == 2
        names = [p["name"] for p in perms]
        assert "Full Disk Access" in names
        assert any("Automation" in n for n in names)

    def test_skill_registry_entry_has_required_fields(self):
        entry = ii.SKILL_REGISTRY_ENTRY
        assert entry["id"] == ii.IMESSAGE_SKILL_ID
        assert entry["kind"] == ii.IMESSAGE_SKILL_KIND
        assert "display_name" in entry


# ── 2026-06-04 bundled-plugin rewire ──────────────────────────────────────────
# The OC wiring stages were added when iMessage was re-added to the catalog
# alongside the bundled @openclaw/imessage plugin (PR following
# internal/openclaw-coverage-audit-2026-06-04.md). These tests pin the new
# states (not_wired_to_oc / oc_probe_failed) + the load-bearing rule that
# ``active`` requires explicit probe success.


class TestResolveStatusBundledPlugin:
    """OC channel-block + live probe gate the ``active`` transition."""

    def test_handle_set_but_no_oc_block_returns_not_wired_to_oc(self):
        """Pre-rewire bots have handle set via the legacy marker but no
        channels.imessage block yet. resolve_status must surface them as
        not_wired_to_oc so the UI offers a one-click finish-setup."""
        status = _make_resolve(
            handle="me@icloud.com",
            oc_channel=None,
            oc_plugin_entry=None,
        )
        assert status.status == "not_wired_to_oc"
        assert status.oc_channel_wired is False
        assert status.oc_plugin_enabled is False
        assert status.oc_probe_ok is False

    def test_channel_block_present_plugin_disabled_returns_not_wired(self):
        """Channel block exists but plugins.entries.imessage.enabled is
        False — half-wired state shouldn't read as active."""
        status = _make_resolve(
            handle="me@icloud.com",
            oc_channel={"enabled": True, "handle": "me@icloud.com"},
            oc_plugin_entry={"enabled": False},
        )
        assert status.status == "not_wired_to_oc"

    def test_wired_but_probe_fails_returns_oc_probe_failed(self):
        """The load-bearing F3 rule: never return ``active`` from config
        presence alone. If the probe says not-connected, the status must
        reflect that — not silently report active."""
        status = _make_resolve(
            handle="me@icloud.com",
            oc_probe={"connected": False, "error": "not_connected",
                      "detail": "Messages.app signed out"},
        )
        assert status.status == "oc_probe_failed"
        assert status.oc_channel_wired is True
        assert status.oc_plugin_enabled is True
        assert status.oc_probe_ok is False
        assert "signed out" in (status.oc_probe_detail or "")

    def test_wired_plus_probe_ok_returns_active(self):
        """All five stages green → active. This is the only state that
        lets the catalog read green; matches the audit's F3 mandate."""
        status = _make_resolve(handle="me@icloud.com")
        assert status.status == "active"
        assert status.oc_channel_wired is True
        assert status.oc_plugin_enabled is True
        assert status.oc_probe_ok is True

    def test_oc_block_handle_overrides_marker(self):
        """When channels.imessage.handle is set, it's authoritative — the
        legacy marker file's handle is only a fallback."""
        status = _make_resolve(
            handle="legacy@icloud.com",  # filesystem marker
            oc_channel={"enabled": True, "handle": "oc-block@icloud.com"},
        )
        assert status.imessage_handle == "oc-block@icloud.com"

    def test_oc_block_allow_from_overrides_marker(self):
        """allowFrom from the OC block wins over allowed_senders from the
        legacy marker — same precedence as handle."""
        status = _make_resolve(
            handle="me@icloud.com",
            config={"handle": "me@icloud.com", "allowed_senders": ["+1111"]},
            oc_channel={"enabled": True, "handle": "me@icloud.com",
                        "allowFrom": ["+2222", "+3333"]},
        )
        assert status.allowed_senders == ["+2222", "+3333"]

    def test_probe_exception_doesnt_crash(self):
        """If the probe helper raises (CLI hang, exec missing, etc.),
        resolve_status must surface oc_probe_failed with the exception
        captured — NEVER report ``active``."""

        def raising_probe(bot_id):
            raise RuntimeError("openclaw cli timeout")

        status = ii.resolve_status(
            "testbot",
            check_tcc_fda=lambda: True,
            check_tcc_automation=lambda: True,
            check_messages_running=lambda: True,
            check_signed_in=lambda: (True, "me@icloud.com"),
            read_config=lambda bot_id: {"handle": "me@icloud.com"},
            read_oc_block=lambda bot_id: (
                {"enabled": True, "handle": "me@icloud.com"},
                {"enabled": True},
                None,
            ),
            probe_oc_channel=raising_probe,
        )
        assert status.status == "oc_probe_failed"
        assert "openclaw cli timeout" in (status.oc_probe_detail or "")


class TestBuildInstallPlanBundledPlugin:
    """The wire_oc_channel + reprobe steps appear in the right states."""

    def test_not_wired_to_oc_offers_wire_step(self):
        """Pre-rewire bot (handle set, OC block missing) gets a one-click
        finish-setup step that calls /set-handle with rewire=True."""
        status = ii.InstallStatus(
            bot_id="b", status="not_wired_to_oc",
            tcc_fda_granted=True, tcc_automation_granted=True,
            messages_app_running=True, signed_in=True,
            imessage_handle="me@icloud.com",
            allowed_senders=["+15551234567"],
        )
        plan = ii.build_install_plan(status)
        assert len(plan) == 1
        assert plan[0].id == "wire_oc_channel"
        assert plan[0].payload["handle"] == "me@icloud.com"
        assert plan[0].payload["allowed_senders"] == ["+15551234567"]
        assert plan[0].payload["rewire"] is True

    def test_oc_probe_failed_offers_reprobe_step(self):
        """Transient probe failure → re-probe button, not a re-wire."""
        status = ii.InstallStatus(
            bot_id="b", status="oc_probe_failed",
            tcc_fda_granted=True, tcc_automation_granted=True,
            messages_app_running=True, signed_in=True,
            imessage_handle="me@icloud.com",
            oc_channel_wired=True, oc_plugin_enabled=True,
        )
        plan = ii.build_install_plan(status)
        assert len(plan) == 1
        assert plan[0].id == "reprobe"

    def test_active_returns_empty_plan_with_oc_fields(self):
        status = ii.InstallStatus(
            bot_id="b", status="active",
            tcc_fda_granted=True, tcc_automation_granted=True,
            messages_app_running=True, signed_in=True,
            imessage_handle="me@icloud.com",
            oc_channel_wired=True, oc_plugin_enabled=True, oc_probe_ok=True,
        )
        assert ii.build_install_plan(status) == []


class TestEnableChannelInOcConfig:
    """Pin the load-bearing wiring helper that closes the audit's 3 failures."""

    def test_writes_minimal_channels_and_plugin_blocks(self, monkeypatch):
        """A fresh openclaw.json with no channels.imessage gets the full
        block written + plugins.entries.imessage.enabled=True."""
        captured: dict = {}
        monkeypatch.setattr(
            ii._oc_common, "read_oc_config",
            lambda bot_id: ({}, None),
        )
        def _capture_write(bot_id, cfg):
            captured["cfg"] = cfg
            return True, None
        monkeypatch.setattr(ii._oc_common, "write_oc_config", _capture_write)

        ok, err = ii.enable_channel_in_oc_config(
            "testbot", "me@icloud.com", allowed_senders=["+15551234567"],
        )
        assert ok and err is None
        cfg = captured["cfg"]
        assert cfg["channels"]["imessage"]["enabled"] is True
        assert cfg["channels"]["imessage"]["handle"] == "me@icloud.com"
        assert cfg["channels"]["imessage"]["service"] == "auto"
        assert cfg["channels"]["imessage"]["dbPath"]
        assert cfg["channels"]["imessage"]["allowFrom"] == ["+15551234567"]
        assert cfg["plugins"]["entries"]["imessage"]["enabled"] is True

    def test_preserves_operator_set_fields(self, monkeypatch):
        """Existing operator-set keys (e.g. dmPolicy) survive a re-wire."""
        existing = {
            "channels": {"imessage": {"enabled": False,
                                       "dmPolicy": "approve_only",
                                       "groups": {"foo": {}}}},
            "plugins": {"entries": {"imessage": {"enabled": False}}},
        }
        captured: dict = {}
        monkeypatch.setattr(ii._oc_common, "read_oc_config",
                            lambda bot_id: (existing, None))
        def _capture_write(bot_id, cfg):
            captured["cfg"] = cfg
            return True, None
        monkeypatch.setattr(ii._oc_common, "write_oc_config", _capture_write)

        ok, _ = ii.enable_channel_in_oc_config("testbot", "me@icloud.com")
        cfg = captured["cfg"]
        # Install flow overwrites enabled+handle but preserves operator fields
        assert cfg["channels"]["imessage"]["enabled"] is True
        assert cfg["channels"]["imessage"]["handle"] == "me@icloud.com"
        assert cfg["channels"]["imessage"]["dmPolicy"] == "approve_only"
        assert "foo" in cfg["channels"]["imessage"]["groups"]
        # And re-enables the plugin
        assert cfg["plugins"]["entries"]["imessage"]["enabled"] is True

    def test_empty_handle_fails(self, monkeypatch):
        """Empty/whitespace handle is rejected before any disk write."""
        called = {"read": False, "write": False}
        monkeypatch.setattr(ii._oc_common, "read_oc_config",
                            lambda bot_id: (called.__setitem__("read", True), {})[1] or ({}, None))
        monkeypatch.setattr(ii._oc_common, "write_oc_config",
                            lambda bot_id, cfg: (called.__setitem__("write", True), True, None)[1:])

        ok, err = ii.enable_channel_in_oc_config("testbot", "   ")
        assert ok is False
        assert err == "handle_empty"
        assert called["write"] is False  # no disk write attempted

    def test_oc_read_failure_propagates(self, monkeypatch):
        """If openclaw.json can't be read, surface the error rather than
        silently writing a blank config."""
        monkeypatch.setattr(ii._oc_common, "read_oc_config",
                            lambda bot_id: (None, "oc_read_failed: ENOENT"))
        ok, err = ii.enable_channel_in_oc_config("testbot", "me@icloud.com")
        assert ok is False
        assert "ENOENT" in (err or "")


class TestRevokeAccount:
    """Symmetric revoke — the audit's F2 cross-cutting fix."""

    def test_revoke_disables_channel_and_plugin(self, monkeypatch):
        """Revoke clears channels.imessage.enabled + plugins.entries
        .imessage.enabled, then kickstarts. Mirrors enable's writes."""
        existing = {
            "channels": {"imessage": {"enabled": True, "handle": "x@y.com"}},
            "plugins": {"entries": {"imessage": {"enabled": True}}},
        }
        captured: dict = {}
        monkeypatch.setattr(ii._oc_common, "read_oc_config",
                            lambda bot_id: (existing, None))
        def _capture_write(bot_id, cfg):
            captured["cfg"] = cfg
            return True, None
        monkeypatch.setattr(ii._oc_common, "write_oc_config", _capture_write)
        monkeypatch.setattr(ii._oc_common, "kickstart_gateway",
                            lambda bot_id: (True, None))
        monkeypatch.setattr(
            "evolve_admin.config.bot_home",
            lambda bot_id: Path("/nonexistent-path-no-marker-cleanup-noop"),
        )

        ok, err = ii.revoke_account("testbot")
        assert ok and err is None
        assert captured["cfg"]["channels"]["imessage"]["enabled"] is False
        assert captured["cfg"]["plugins"]["entries"]["imessage"]["enabled"] is False

    def test_revoke_kickstart_failure_propagates(self, monkeypatch):
        """If kickstart fails, the revoke is reported as failed even
        though the config write succeeded — operator needs to know."""
        monkeypatch.setattr(ii._oc_common, "read_oc_config",
                            lambda bot_id: ({"channels": {"imessage": {}}}, None))
        monkeypatch.setattr(ii._oc_common, "write_oc_config",
                            lambda bot_id, cfg: (True, None))
        monkeypatch.setattr(ii._oc_common, "kickstart_gateway",
                            lambda bot_id: (False, "launchctl_failed"))
        monkeypatch.setattr(
            "evolve_admin.config.bot_home",
            lambda bot_id: Path("/nonexistent-path"),
        )
        ok, err = ii.revoke_account("testbot")
        assert ok is False
        assert "launchctl_failed" in (err or "")


class TestAccessPanelHonest:
    """The May audit's F5 rule: no aspirational present-tense capability
    promises. Every line in ``will`` must map to something OC's bundled
    plugin actually delivers."""

    def test_will_list_does_not_promise_bot_initiated_outreach(self):
        """The bundled plugin replies in existing conversations but
        doesn't proactively start new ones — make sure we don't promise
        what OC can't deliver."""
        will_text = " ".join(ii.IMESSAGE_ACCESS_PANEL.get("will", [])).lower()
        # These were V2.1-6 promises that the bundled plugin can't keep
        # in v1 of the rewire (Layer-1 tool exposure is deferred).
        assert "send imessages to contacts you specify" not in will_text
        assert "look up contacts you've previously messaged" not in will_text

    def test_wont_list_includes_bot_initiated_disclaimer(self):
        wont_text = " ".join(ii.IMESSAGE_ACCESS_PANEL.get("wont", [])).lower()
        # Plex-test honesty — operator sees what the bot can't do upfront.
        assert "start" in wont_text and "new conversations" in wont_text

    def test_registry_entry_carries_kind_and_platforms(self):
        """The catalog dispatchers in web/routes_admin.py read kind +
        platforms off the registry entry. ``platforms`` is the catalog
        data the channel-matrix platform filter consumes
        (design-linux-port §8 — a Linux pod never offers iMessage)."""
        entry = ii.SKILL_REGISTRY_ENTRY
        assert entry.get("kind") == ii.IMESSAGE_SKILL_KIND
        assert entry.get("platforms") == ["macos"]
