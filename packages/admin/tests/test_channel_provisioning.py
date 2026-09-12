"""Tests for evolve_admin.channel_provisioning — the add-a-channel operation.

M1-B4b. Every side effect is injected: no bot user, no sudo, no openclaw
binary, no filesystem. Credentials in this file are obviously-fake
placeholders.

The behaviours pinned here are the ones that would silently rot:

  * core-vs-plugin is decided off the registry's ``install`` column, so a
    Telegram add never shells an npm install and a Slack add always does;
  * the ``channels`` map is MERGED, never replaced, and operator-set fields
    survive a redo;
  * a second identical call writes nothing (idempotence);
  * nothing restarts a gateway without ``restart_gateway=True``.
"""

from __future__ import annotations

import copy

import pytest

from evolve_admin import channel_provisioning as cp
from evolve_admin import channel_registry as cr


# Placeholder credentials — never real tokens.
FAKE_TOKEN = "placeholder-not-a-real-token"


def _core_channel() -> cr.ChannelSpec:
    """A registry row that needs no plugin install."""
    rows = cr.where(
        lambda c: c.install == cr.INSTALL_CORE and c.messaging_integration
    )
    assert rows, "registry has no core messaging channel"
    return rows[0]


def _plugin_channel() -> cr.ChannelSpec:
    """A registry row that DOES need a plugin install."""
    rows = cr.where(
        lambda c: c.install == cr.INSTALL_OFFICIAL_PLUGIN and c.messaging_integration
    )
    assert rows, "registry has no official-plugin messaging channel"
    return rows[0]


class _Harness:
    """Injectable stand-in for every side effect the operation has."""

    def __init__(self, cfg: dict | None = None, installed: set[str] | None = None):
        self.cfg = cfg if cfg is not None else {}
        self.installed = set(installed or ())
        self.writes: list[dict] = []
        self.installs: list[tuple[str, str]] = []
        self.restarts: list[str] = []
        self.install_ok = True
        self.write_ok = True

    def read(self, bot_id):
        return copy.deepcopy(self.cfg), None

    def write(self, bot_id, cfg):
        if not self.write_ok:
            return False, "oc_write_failed: injected"
        self.writes.append(copy.deepcopy(cfg))
        self.cfg = copy.deepcopy(cfg)
        return True, None

    def list_installed(self, bot_id):
        return set(self.installed)

    def install(self, bot_id, npm_package):
        self.installs.append((bot_id, npm_package))
        if not self.install_ok:
            return False, "injected install failure"
        self.installed.add(npm_package.split("/")[-1])
        return True, None

    def restart(self, bot_id):
        self.restarts.append(bot_id)
        return True, None

    def call(self, channel_id, **kw):
        kw.setdefault("read_config", self.read)
        kw.setdefault("write_config", self.write)
        kw.setdefault("installed_plugin_ids", self.list_installed)
        kw.setdefault("plugin_installer", self.install)
        kw.setdefault("restart", self.restart)
        return cp.add_channel_to_bot("placeholder-bot", channel_id, **kw)


# ── Registry resolution ─────────────────────────────────────────────────


def test_unknown_channel_is_rejected_without_touching_disk():
    h = _Harness()
    out = h.call("not-a-real-channel")
    assert out.ok is False
    assert "unknown channel" in (out.error or "")
    assert h.writes == []


def test_non_openclaw_delivery_id_is_rejected():
    """email / webhook are labels, not channels OC runs."""
    rows = cr.where(lambda c: c.install is None)
    assert rows, "registry has no non-OC delivery id"
    h = _Harness()
    out = h.call(rows[0].id)
    assert out.ok is False
    assert "not an OpenClaw channel" in (out.error or "")
    assert h.writes == []


def test_channel_id_is_normalised_through_the_registry():
    spec = _core_channel()
    h = _Harness()
    out = h.call(spec.id.upper())
    assert out.ok is True
    assert out.channel_id == spec.id


# ── core vs plugin, decided off the registry ────────────────────────────


def test_core_channel_never_installs_a_plugin():
    spec = _core_channel()
    h = _Harness()
    out = h.call(spec.id)
    assert out.ok is True
    assert out.plugin_state == cp.PLUGIN_NOT_REQUIRED
    assert h.installs == []


def test_plugin_channel_installs_the_registry_named_package():
    spec = _plugin_channel()
    h = _Harness()
    out = h.call(spec.id)
    assert out.ok is True
    assert out.plugin_state == cp.PLUGIN_INSTALLED
    assert h.installs == [("placeholder-bot", spec.oc_plugin_id)]


def test_plugin_channel_skips_install_when_already_present():
    spec = _plugin_channel()
    h = _Harness(installed={spec.id})
    out = h.call(spec.id)
    assert out.plugin_state == cp.PLUGIN_ALREADY_INSTALLED
    assert h.installs == []


def test_plugin_install_failure_aborts_before_any_config_write():
    spec = _plugin_channel()
    h = _Harness()
    h.install_ok = False
    out = h.call(spec.id)
    assert out.ok is False
    assert out.plugin_state == cp.PLUGIN_FAILED
    assert h.writes == []


def test_install_plugin_false_still_writes_the_channel_block():
    spec = _plugin_channel()
    h = _Harness()
    out = h.call(spec.id, install_plugin=False)
    assert out.ok is True
    assert out.plugin_state == cp.PLUGIN_SKIPPED
    assert h.installs == []
    assert spec.id in h.cfg["channels"]


def test_needs_plugin_install_matches_the_install_column():
    for spec in cr.all_channels():
        expected = spec.install in (
            cr.INSTALL_OFFICIAL_PLUGIN, cr.INSTALL_EXTERNAL_PLUGIN,
        )
        assert cp.channel_needs_plugin_install(spec) is expected, spec.id


# ── merge, never replace ────────────────────────────────────────────────


def test_existing_channels_are_preserved():
    spec = _plugin_channel()
    other = _core_channel()
    h = _Harness({"channels": {other.id: {"enabled": True, "botToken": FAKE_TOKEN}}})
    out = h.call(spec.id)
    assert out.ok is True
    assert set(h.cfg["channels"]) == {other.id, spec.id}
    assert h.cfg["channels"][other.id]["botToken"] == FAKE_TOKEN


def test_operator_set_fields_survive_a_redo():
    spec = _core_channel()
    h = _Harness({
        "channels": {spec.id: {"enabled": False, "dmPolicy": "open",
                               "operatorOnly": "keep-me"}},
    })
    out = h.call(spec.id)
    assert out.ok is True
    block = h.cfg["channels"][spec.id]
    # Only ``enabled`` is ours to overwrite — "add the channel" means on.
    assert block["enabled"] is True
    assert block["dmPolicy"] == "open"
    assert block["operatorOnly"] == "keep-me"


def test_unrelated_config_sections_are_untouched():
    spec = _core_channel()
    h = _Harness({"gateway": {"mode": "local"}, "agents": {"defaults": {}}})
    h.call(spec.id)
    assert h.cfg["gateway"] == {"mode": "local"}
    assert h.cfg["agents"] == {"defaults": {}}


def test_plugin_entry_is_enabled():
    spec = _plugin_channel()
    h = _Harness()
    h.call(spec.id)
    assert h.cfg["plugins"]["entries"][spec.id]["enabled"] is True


def test_caller_supplied_channel_fields_are_seeded_not_forced():
    spec = _core_channel()
    h = _Harness({"channels": {spec.id: {"mode": "operator-chose"}}})
    h.call(spec.id, channel_fields={"mode": "socket", "extra": 1})
    block = h.cfg["channels"][spec.id]
    assert block["mode"] == "operator-chose"
    assert block["extra"] == 1


def test_malformed_channels_map_is_refused():
    spec = _core_channel()
    h = _Harness({"channels": ["not", "an", "object"]})
    out = h.call(spec.id)
    assert out.ok is False
    assert h.writes == []


def test_unreadable_config_is_refused():
    spec = _core_channel()
    h = _Harness()
    h.read = lambda bot_id: (None, "sudo cat failed")  # type: ignore[assignment]
    out = h.call(spec.id)
    assert out.ok is False
    assert out.error == "sudo cat failed"


def test_write_failure_surfaces_as_not_ok():
    spec = _core_channel()
    h = _Harness()
    h.write_ok = False
    out = h.call(spec.id)
    assert out.ok is False
    assert out.config_changed is False


# ── idempotence ─────────────────────────────────────────────────────────


def test_second_identical_call_writes_nothing():
    spec = _core_channel()
    h = _Harness()
    first = h.call(spec.id)
    assert first.config_changed is True
    assert len(h.writes) == 1

    second = h.call(spec.id)
    assert second.ok is True
    assert second.config_changed is False
    assert second.restart_required is False
    assert len(h.writes) == 1


# ── credentials go through the mirror registry, never by hand ───────────


def test_credential_is_applied_through_the_registry_helper(monkeypatch):
    spec = _core_channel()
    h = _Harness()
    seen: list[tuple] = []

    def _fake_apply(cfg, provider, field_key, value):
        seen.append((provider, field_key, value))
        cfg.setdefault("channels", {}).setdefault(provider, {})["botToken"] = value
        return True

    monkeypatch.setattr(
        "evolve_admin.web.server._apply_credential_to_oc_dict", _fake_apply,
    )
    out = h.call(spec.id, credential=FAKE_TOKEN)
    assert out.credential_applied is True
    assert out.credential_pending is False
    assert seen == [(spec.id, "bot_token", FAKE_TOKEN)]


def test_unmapped_credential_is_reported_not_hand_written(monkeypatch):
    """No registry mapping → we refuse to guess a token key."""
    spec = _core_channel()
    h = _Harness()
    monkeypatch.setattr(
        "evolve_admin.web.server._apply_credential_to_oc_dict",
        lambda cfg, provider, field_key, value: False,
    )
    out = h.call(spec.id, credential=FAKE_TOKEN)
    assert out.ok is True
    assert out.credential_applied is False
    assert out.credential_pending is True
    block = h.cfg["channels"][spec.id]
    assert FAKE_TOKEN not in str(block)
    assert any("_RUNTIME_MIRROR_PATH" in n for n in out.notes)


def test_missing_credential_is_flagged_pending():
    spec = _core_channel()
    h = _Harness()
    out = h.call(spec.id)
    assert out.credential_pending is True


def test_existing_credential_is_not_flagged_pending():
    spec = _core_channel()
    h = _Harness({"channels": {spec.id: {"enabled": True, "botToken": FAKE_TOKEN}}})
    out = h.call(spec.id)
    assert out.credential_pending is False


# ── restarts are opt-in ─────────────────────────────────────────────────


def test_no_restart_by_default():
    spec = _core_channel()
    h = _Harness()
    out = h.call(spec.id)
    assert h.restarts == []
    assert out.gateway_restarted is False
    assert out.restart_required is True


def test_restart_only_on_explicit_opt_in():
    spec = _core_channel()
    h = _Harness()
    out = h.call(spec.id, restart_gateway=True)
    assert h.restarts == ["placeholder-bot"]
    assert out.gateway_restarted is True
    assert out.restart_required is False


def test_restart_failure_does_not_fail_the_write():
    spec = _core_channel()
    h = _Harness()
    h.restart = lambda bot_id: (False, "launchctl kickstart failed")  # type: ignore[assignment]
    out = h.call(spec.id, restart_gateway=True)
    assert out.ok is True
    assert out.config_changed is True
    assert out.gateway_restarted is False


# ── projections + defaults ──────────────────────────────────────────────


def test_provisionable_channels_excludes_non_oc_delivery_ids():
    ids = {c.id for c in cp.provisionable_channels()}
    for spec in cr.all_channels():
        if spec.install is None or not spec.messaging_integration:
            assert spec.id not in ids
        else:
            assert spec.id in ids


def test_default_fields_are_capability_derived():
    for spec in cp.provisionable_channels():
        fields = cp.default_channel_fields(spec)
        assert fields["enabled"] is True
        assert ("groupPolicy" in fields) == (
            spec.supports_groups and spec.supports_allowlist
        )
        assert ("dmPolicy" in fields) == (
            spec.supports_dms and spec.supports_allowlist
        )


def test_no_bot_id_is_rejected():
    out = cp.add_channel_to_bot("  ", _core_channel().id)
    assert out.ok is False
    assert "bot_id required" in (out.error or "")


def test_outcome_serializes():
    spec = _core_channel()
    h = _Harness()
    d = h.call(spec.id).to_dict()
    assert d["channel_id"] == spec.id
    assert set(d) >= {"ok", "plugin_state", "restart_required", "notes"}


@pytest.mark.parametrize("spec", list(cp.provisionable_channels()), ids=lambda s: s.id)
def test_every_provisionable_channel_can_be_added(spec):
    """A new registry row is addable for free — no per-channel code."""
    h = _Harness()
    out = h.call(spec.id)
    assert out.ok is True, out.error
    assert h.cfg["channels"][spec.id]["enabled"] is True
