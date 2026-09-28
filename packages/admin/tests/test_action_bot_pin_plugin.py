"""tests/test_action_bot_pin_plugin.py — action.bot.pin_plugin_version.

These tests cover the tool added in response to the 2026-05-26
operator transcript where evo, lacking a registered path for pinning
plugin versions across bots, fabricated ``sudo -u <bot> openclaw
plugins install --pin ...`` (the shape PR #1626 had just patched the
inspector to reject). Diagnosis chain: PR #1492 / #1503 / #1623 / #1626
/ this PR.

Fixture strategy: mirror ``test_evo_backup_tools.py`` and
``test_action_plugin.py`` — stub the I/O seams (``_read_openclaw_json``,
``safe_write_bot_config``, ``_kickstart_gateway``, ``set_intent``) so we
exercise the routing logic without touching ``/Users/<bot>/.openclaw/``
or shelling out.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))


# ─── Fixtures ────────────────────────────────────────────────────────────────


def _seed_network(
    tmp_path: Path,
    bots: dict[str, dict] | None = None,
) -> Path:
    """Write a network.json with the given bots. Default seeds the six
    bots from the original transcript so the multi-bot batching path
    has a realistic shape."""
    if bots is None:
        bots = {
            "team_bot_a": {"role": "member"},
            "team_bot_c": {"role": "member"},
            "personal_bot": {"role": "member"},
            "admin_bot": {"role": "member"},
            "security_bot": {"role": "member"},
            "team_bot_b": {"role": "member", "user": "personal_bot_user"},
        }
    p = tmp_path / "network.json"
    p.write_text(json.dumps({"networkId": "test-pod", "bots": bots}))
    return p


def _oc_cfg(spec: str = "@openclaw/brave-plugin") -> dict:
    """Minimal openclaw.json shape with one plugins.installs entry."""
    return {
        "plugins": {
            "installs": {
                "brave": {
                    "source": "npm",
                    "spec": spec,
                    "installPath": "/Users/test/.openclaw/extensions/brave",
                },
            },
        },
    }


def _install_fakes(
    monkeypatch,
    *,
    oc_by_user: dict[str, dict],
    write_ok: bool = True,
    write_err: str = "",
    kickstart_ok: bool = True,
    kickstart_err: str = "",
):
    """Patch the I/O seams the handler talks to. Returns a captured-state
    dict so tests can assert which subprocess calls landed.

    ``oc_by_user`` maps bot_user → openclaw.json dict. The fake reader
    returns the dict; the fake writer captures the dict written back.
    """
    from evolve_admin.evo.tools import action_bot_pin_plugin as mod

    captured: dict = {"writes": [], "kickstarts": [], "intents": []}

    def _fake_read(bot_user):
        cfg = oc_by_user.get(bot_user)
        if cfg is None:
            return None, f"no fake config for {bot_user}"
        # Return a copy so handler mutations don't leak across reads.
        return json.loads(json.dumps(cfg)), None

    monkeypatch.setattr(mod, "_read_openclaw_json", _fake_read)

    def _fake_safe_write(bot_id, new_config, reason="", bot_user=None):
        captured["writes"].append({
            "bot_id": bot_id,
            "bot_user": bot_user,
            "reason": reason,
            "config": new_config,
        })
        return write_ok, write_err

    # safe_write_bot_config is imported inside the handler — patch the
    # source module so the late import resolves to our fake.
    import evolve_admin.deploy as deploy_mod
    monkeypatch.setattr(deploy_mod, "safe_write_bot_config", _fake_safe_write)

    def _fake_kickstart(bot_id):
        captured["kickstarts"].append(bot_id)
        return kickstart_ok, kickstart_err

    monkeypatch.setattr(mod, "_kickstart_gateway", _fake_kickstart)

    def _fake_set_intent(bot_id, field_path, value, **kwargs):
        captured["intents"].append({
            "bot_id": bot_id,
            "field_path": field_path,
            "value": value,
            "reason": kwargs.get("reason"),
            "set_by": kwargs.get("set_by"),
        })
        return "intent-fake"

    import evolve_admin.config_intent as intent_mod
    monkeypatch.setattr(intent_mod, "set_intent", _fake_set_intent)

    return captured


# ─── Version validation ──────────────────────────────────────────────────────


@pytest.mark.parametrize("version", [
    "2026.5.22",
    "1.2.3",
    "1.2.3-beta.1",
    "1.2.3-rc.4",
    "10.0.0",
])
def test_exact_version_accepted(version):
    from evolve_admin.evo.tools.action_bot_pin_plugin import _is_exact_version
    assert _is_exact_version(version), f"{version!r} should be accepted"


@pytest.mark.parametrize("version", [
    "^1.2.3",
    "~1.2.3",
    ">=1.2.3",
    "latest",
    "next",
    "beta",
    "*",
    "1.x",
    "1.2",          # missing patch
    "",
    "v1.2.3",       # leading 'v' — npm strips, audit may still flag
    "1.2.3 ",       # trailing whitespace — handler strips before validating,
                    # but the raw helper should reject
])
def test_non_exact_version_rejected(version):
    from evolve_admin.evo.tools.action_bot_pin_plugin import _is_exact_version
    assert not _is_exact_version(version), f"{version!r} should be rejected"


# ─── Spec helpers ────────────────────────────────────────────────────────────


def test_spec_to_npm_package_strips_version_suffix():
    from evolve_admin.evo.tools.action_bot_pin_plugin import _spec_to_npm_package
    assert _spec_to_npm_package("@openclaw/brave-plugin@2026.5.22") == "@openclaw/brave-plugin"
    assert _spec_to_npm_package("brave@1.2.3") == "brave"
    assert _spec_to_npm_package("@openclaw/brave-plugin") == "@openclaw/brave-plugin"
    assert _spec_to_npm_package("plain-name") == "plain-name"


def test_resolve_install_key_direct_match():
    from evolve_admin.evo.tools.action_bot_pin_plugin import _resolve_install_key
    installs = {"brave": {"spec": "@openclaw/brave-plugin"}}
    key, rec = _resolve_install_key(installs, "brave")
    assert key == "brave"
    assert rec == installs["brave"]


def test_resolve_install_key_npm_pkg_match():
    """Operators say '@openclaw/brave-plugin' more often than 'brave'.
    The resolver must fall back to a spec-prefix scan."""
    from evolve_admin.evo.tools.action_bot_pin_plugin import _resolve_install_key
    installs = {
        "brave": {"spec": "@openclaw/brave-plugin@2026.5.0"},
        "gh": {"spec": "@openclaw/github-plugin@2026.5.0"},
    }
    key, rec = _resolve_install_key(installs, "@openclaw/brave-plugin")
    assert key == "brave"
    assert rec == installs["brave"]


def test_resolve_install_key_no_match():
    from evolve_admin.evo.tools.action_bot_pin_plugin import _resolve_install_key
    installs = {"brave": {"spec": "@openclaw/brave-plugin@2026.5.0"}}
    key, rec = _resolve_install_key(installs, "ghost-plugin")
    assert key is None
    assert rec is None


# ─── Validate ────────────────────────────────────────────────────────────────


def test_validate_rejects_empty_pins(tmp_path):
    from evolve_admin.evo.tools import action_bot_pin_plugin as mod
    network_path = _seed_network(tmp_path)
    r = mod._pin_validate(network_path=network_path, pins=[])
    assert r["ok"] is False
    assert "non-empty" in r["reason"]


def test_validate_rejects_unknown_bot(tmp_path):
    from evolve_admin.evo.tools import action_bot_pin_plugin as mod
    network_path = _seed_network(tmp_path)
    r = mod._pin_validate(network_path=network_path, pins=[
        {"bot_id": "ghost", "plugin_name": "brave", "version": "2026.5.22"},
    ])
    assert r["ok"] is False
    assert "ghost" in r["reason"]
    assert "not in network.json" in r["reason"]


def test_validate_rejects_version_range(tmp_path):
    from evolve_admin.evo.tools import action_bot_pin_plugin as mod
    network_path = _seed_network(tmp_path)
    r = mod._pin_validate(network_path=network_path, pins=[
        {"bot_id": "team_bot_a", "plugin_name": "brave", "version": "^2026.5.22"},
    ])
    assert r["ok"] is False
    assert "exact pin" in r["reason"]


def test_validate_happy_path(tmp_path):
    from evolve_admin.evo.tools import action_bot_pin_plugin as mod
    network_path = _seed_network(tmp_path)
    r = mod._pin_validate(network_path=network_path, pins=[
        {"bot_id": "team_bot_a", "plugin_name": "brave", "version": "2026.5.22"},
        {"bot_id": "team_bot_b", "plugin_name": "@openclaw/brave-plugin",
         "version": "2026.5.22"},
    ])
    assert r["ok"] is True
    assert r["context"]["pin_count"] == 2
    assert set(r["context"]["bots_touched"]) == {"team_bot_a", "team_bot_b"}


# ─── Handler: success paths ──────────────────────────────────────────────────


def test_handler_pins_single_bot(monkeypatch, tmp_path):
    """Happy path: read openclaw.json, mutate plugins.installs.brave.spec,
    write back, kickstart, record intent."""
    from evolve_admin.evo.tools import action_bot_pin_plugin as mod
    network_path = _seed_network(tmp_path)
    captured = _install_fakes(
        monkeypatch,
        oc_by_user={"team_bot_a": _oc_cfg("@openclaw/brave-plugin")},
    )

    r = mod._pin_handler(
        network_path=network_path,
        pins=[{"bot_id": "team_bot_a", "plugin_name": "brave", "version": "2026.5.22"}],
    )
    assert r["ok"] is True
    assert r["summary"] == {"pinned": 1, "already_pinned": 0, "failed": 0}
    pin_result = r["pins"][0]
    assert pin_result["status"] == "pinned"
    assert pin_result["prior_spec"] == "@openclaw/brave-plugin"
    assert pin_result["new_spec"] == "@openclaw/brave-plugin@2026.5.22"
    assert pin_result["install_key"] == "brave"
    assert pin_result["npm_package"] == "@openclaw/brave-plugin"

    # The written config has the patched spec.
    assert len(captured["writes"]) == 1
    written = captured["writes"][0]["config"]
    assert written["plugins"]["installs"]["brave"]["spec"] == "@openclaw/brave-plugin@2026.5.22"

    # Gateway kickstart happened for team_bot_a.
    assert captured["kickstarts"] == ["team_bot_a"]

    # Intent recorded against the spec field.
    assert len(captured["intents"]) == 1
    intent = captured["intents"][0]
    assert intent["bot_id"] == "team_bot_a"
    assert intent["field_path"] == "plugins.installs.brave.spec"
    assert intent["value"] == "@openclaw/brave-plugin@2026.5.22"
    assert intent["set_by"] == "action.bot.pin_plugin_version"


def test_handler_batches_across_bots(monkeypatch, tmp_path):
    """The operator's natural shape: a single OC release pinning across
    six bots in one call. Each bot writes + kickstarts independently;
    the aggregate summary counts pinned + already_pinned + failed."""
    from evolve_admin.evo.tools import action_bot_pin_plugin as mod
    network_path = _seed_network(tmp_path)
    captured = _install_fakes(
        monkeypatch,
        oc_by_user={
            "team_bot_a": _oc_cfg("@openclaw/brave-plugin"),
            "team_bot_c": _oc_cfg("@openclaw/brave-plugin"),
            "personal_bot": _oc_cfg("@openclaw/brave-plugin"),
            "admin_bot": _oc_cfg("@openclaw/brave-plugin"),
            "security_bot": _oc_cfg("@openclaw/brave-plugin"),
            # team_bot_b runs on the personal_bot_user account — exercise the bot_id !=
            # user resolution path (memory: feedback_bot_id_not_account_name).
            "personal_bot_user": _oc_cfg("@openclaw/brave-plugin"),
        },
    )

    pins = [
        {"bot_id": b, "plugin_name": "@openclaw/brave-plugin", "version": "2026.5.22"}
        for b in ("team_bot_a", "team_bot_c", "personal_bot", "admin_bot", "security_bot", "team_bot_b")
    ]
    r = mod._pin_handler(network_path=network_path, pins=pins)
    assert r["ok"] is True
    assert r["summary"]["pinned"] == 6
    assert r["summary"]["failed"] == 0
    # All six writes landed.
    assert len(captured["writes"]) == 6
    # All six gateways kickstarted — note team_bot_b (bot_id), not personal_bot_user
    # (account). launchctl label is keyed by bot_id.
    assert sorted(captured["kickstarts"]) == sorted([
        "team_bot_a", "team_bot_c", "personal_bot", "admin_bot", "security_bot", "team_bot_b",
    ])
    # Team_bot_b's write was addressed to the personal_bot_user account.
    team_bot_b_write = next(w for w in captured["writes"] if w["bot_id"] == "team_bot_b")
    assert team_bot_b_write["bot_user"] == "personal_bot_user"


def test_handler_idempotent_when_already_pinned(monkeypatch, tmp_path):
    """If the spec already matches the requested version, the tool must
    skip the write + kickstart (idempotent). The aggregate counts the
    skip under ``already_pinned``."""
    from evolve_admin.evo.tools import action_bot_pin_plugin as mod
    network_path = _seed_network(tmp_path)
    captured = _install_fakes(
        monkeypatch,
        oc_by_user={"team_bot_a": _oc_cfg("@openclaw/brave-plugin@2026.5.22")},
    )

    r = mod._pin_handler(
        network_path=network_path,
        pins=[{"bot_id": "team_bot_a", "plugin_name": "brave", "version": "2026.5.22"}],
    )
    assert r["ok"] is True
    assert r["summary"] == {"pinned": 0, "already_pinned": 1, "failed": 0}
    assert r["pins"][0]["status"] == "already_pinned"
    # No write + no kickstart on idempotent calls.
    assert captured["writes"] == []
    assert captured["kickstarts"] == []
    # No intent recorded either — nothing changed.
    assert captured["intents"] == []


# ─── Handler: failure paths ──────────────────────────────────────────────────


def test_handler_rejects_version_range(tmp_path):
    from evolve_admin.evo.tools import action_bot_pin_plugin as mod
    network_path = _seed_network(tmp_path)
    r = mod._pin_handler(
        network_path=network_path,
        pins=[{"bot_id": "team_bot_a", "plugin_name": "brave", "version": "^2026.5.22"}],
    )
    assert r["ok"] is False
    assert r["summary"]["failed"] == 1
    assert r["pins"][0]["status"] == "invalid_version"


def test_handler_unknown_bot_per_pin(monkeypatch, tmp_path):
    """An unknown bot in the pin list surfaces as a per-pin failure —
    doesn't abort the batch. Other pins still run."""
    from evolve_admin.evo.tools import action_bot_pin_plugin as mod
    network_path = _seed_network(tmp_path)
    _install_fakes(
        monkeypatch,
        oc_by_user={"team_bot_a": _oc_cfg("@openclaw/brave-plugin")},
    )

    r = mod._pin_handler(
        network_path=network_path,
        pins=[
            {"bot_id": "team_bot_a", "plugin_name": "brave", "version": "2026.5.22"},
            {"bot_id": "ghost", "plugin_name": "brave", "version": "2026.5.22"},
        ],
    )
    assert r["ok"] is False  # ghost failed
    assert r["summary"]["pinned"] == 1
    assert r["summary"]["failed"] == 1
    team_bot_a_result = next(p for p in r["pins"] if p["bot_id"] == "team_bot_a")
    ghost_result = next(p for p in r["pins"] if p["bot_id"] == "ghost")
    assert team_bot_a_result["status"] == "pinned"
    assert ghost_result["status"] == "unknown_bot"


def test_handler_unknown_plugin_on_bot(monkeypatch, tmp_path):
    """If the bot doesn't have an install record for the named plugin,
    return ``unknown_plugin`` with a clear error — don't silently
    add a new entry."""
    from evolve_admin.evo.tools import action_bot_pin_plugin as mod
    network_path = _seed_network(tmp_path)
    captured = _install_fakes(
        monkeypatch,
        oc_by_user={"team_bot_a": _oc_cfg("@openclaw/brave-plugin")},
    )

    r = mod._pin_handler(
        network_path=network_path,
        pins=[{"bot_id": "team_bot_a", "plugin_name": "@openclaw/ghost-plugin",
               "version": "2026.5.22"}],
    )
    assert r["ok"] is False
    assert r["pins"][0]["status"] == "unknown_plugin"
    # No write/kickstart — the bot config was untouched.
    assert captured["writes"] == []
    assert captured["kickstarts"] == []


def test_handler_write_failure_surfaces(monkeypatch, tmp_path):
    """If safe_write_bot_config returns ok=False, the tool must
    propagate the error verbatim and skip kickstart + intent recording.
    No silent recovery — operator must see the validation failure."""
    from evolve_admin.evo.tools import action_bot_pin_plugin as mod
    network_path = _seed_network(tmp_path)
    captured = _install_fakes(
        monkeypatch,
        oc_by_user={"team_bot_a": _oc_cfg("@openclaw/brave-plugin")},
        write_ok=False,
        write_err="Config validation FAILED: tools.exec.security: Unrecognized key",
    )

    r = mod._pin_handler(
        network_path=network_path,
        pins=[{"bot_id": "team_bot_a", "plugin_name": "brave", "version": "2026.5.22"}],
    )
    assert r["ok"] is False
    assert r["pins"][0]["status"] == "write_failed"
    assert "Unrecognized key" in r["pins"][0]["error"]
    # Kickstart + intent must NOT have run — defensive: a failed write
    # can't pretend the new spec landed.
    assert captured["kickstarts"] == []
    assert captured["intents"] == []


def test_handler_kickstart_failure_is_warning_not_fatal(monkeypatch, tmp_path):
    """If the write lands but kickstart fails, the tool returns ok=True
    with a kickstart_warning — the file is patched, the next explicit
    restart picks it up. Same posture as UpdatePermissionConfigApplier."""
    from evolve_admin.evo.tools import action_bot_pin_plugin as mod
    network_path = _seed_network(tmp_path)
    captured = _install_fakes(
        monkeypatch,
        oc_by_user={"team_bot_a": _oc_cfg("@openclaw/brave-plugin")},
        kickstart_ok=False,
        kickstart_err="rc=113: Could not find service",
    )

    r = mod._pin_handler(
        network_path=network_path,
        pins=[{"bot_id": "team_bot_a", "plugin_name": "brave", "version": "2026.5.22"}],
    )
    assert r["ok"] is True  # the write succeeded
    pin = r["pins"][0]
    assert pin["status"] == "pinned"
    assert "kickstart_warning" in pin
    assert "113" in pin["kickstart_warning"]
    # Intent was still recorded — the operator's choice is the same
    # regardless of whether the gateway picked it up immediately.
    assert len(captured["intents"]) == 1


# ─── Registry + UI alternative wiring ───────────────────────────────────────


def test_tool_registered():
    """Tool must appear in the registry under the expected name with
    write_risky tier. Catches a refactor that drops the ``register()``
    call or accidentally downgrades the risk tier."""
    from evolve_admin.evo.tools import RiskTier, lookup
    t = lookup("action.bot.pin_plugin_version")
    assert t is not None, (
        "action.bot.pin_plugin_version not registered. Did the import "
        "side-effect in evolve_admin.evo.tools.action_bot_pin_plugin "
        "get removed?"
    )
    assert t.risk_tier == RiskTier.WRITE_RISKY, (
        "pin_plugin_version should be WRITE_RISKY — it writes openclaw.json "
        "on multiple bots and kickstarts gateways. Same risk shape as "
        "action.bot.redeploy."
    )
    assert t.validate is not None


def test_inspector_ui_alternative_maps_to_pin_plugin_version():
    """When evo (incorrectly) emits an ``openclaw plugins install --pin``
    shell snippet, the inspector's UI-alternative lookup must point at
    the registered tool — not return None (which would fall through to
    the bare ``hallucinated_cli`` stub the operator originally saw)."""
    from evolve_admin.evo.inspector import lookup_ui_alternative
    blocks = [
        "sudo -u team_bot_a openclaw plugins install --pin @openclaw/brave-plugin@2026.5.22",
    ]
    alt = lookup_ui_alternative(blocks)
    assert alt is not None, (
        "_UI_ALTERNATIVES should have an entry for "
        "'openclaw plugins install --pin' that points at "
        "the pin_plugin_version bot_action. Without it the hallucinated "
        "CLI stub gives the operator no forward path."
    )
    # B7 Phase 2 tool diet: the suggestion teaches the facade calling form.
    assert 'bot_action(action="pin_plugin_version"' in alt
