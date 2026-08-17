"""
Unit tests for evolve plugin config injection and security_bot config repair.

These tests mock subprocess calls so they run without a real macOS environment.
Run with: python -m pytest packages/admin/tests/test_install.py -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock, patch, call

import pytest

# ── Path setup ────────────────────────────────────────────────────────────────
_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.deploy import ensure_plugin_config, repair_security_bot_config  # noqa: E402


@pytest.fixture(autouse=True)
def _stub_plugin_signature(monkeypatch):
    """install_oc_plugin verifies the deployed plugin's stamped content
    digests before installing. Tests in this
    file don't exercise the signature path (they mock the install command
    itself), so stub the verifier to assume success. The signature path has
    its own dedicated test class (``TestInstallOcPluginSignedBypass`` below)
    and direct unit tests in ``test_plugin_signature.py``.
    """
    import evolve_admin.deploy as _deploy
    monkeypatch.setattr(
        _deploy, "verify_plugin_signature",
        lambda _plugin_dir: (True, ""), raising=True,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_oc_json(
    extra_plugins: dict | None = None,
    with_cost_defaults: bool = False,
) -> str:
    """Return a minimal openclaw.json with optional extra plugin entries.

    ``with_cost_defaults=True`` treats the fixture as "fully correct" — seeds
    every field ensure_plugin_config would otherwise inject/repair, so tests
    asserting "no write should happen" can represent that state accurately.
    Kept under one flag because every new completeness check added to
    ensure_plugin_config needs to be represented here too.
    """
    agent_defaults: dict = {"workspace": "/Users/admin_bot/.openclaw/workspace"}
    gateway: dict = {"port": 3000}
    cfg: dict = {
        "agents": {"defaults": agent_defaults},
        "gateway": gateway,
        "plugins": {"entries": extra_plugins or {}},
    }
    if with_cost_defaults:
        agent_defaults.update({
            "model": {"primary": "anthropic/claude-sonnet-4-6", "fallbacks": []},
            "thinkingDefault": "off",
            "heartbeat": {
                "isolatedSession": True,
                "lightContext": True,
                "model": "anthropic/claude-haiku-4-5",
            },
            "contextPruning": {"mode": "cache-ttl", "ttl": "4h", "keepLastAssistants": 5},
            "compaction": {
                "mode": "safeguard",
                "reserveTokensFloor": 50000,
                "memoryFlush": {"enabled": True, "softThresholdTokens": 10000},
            },
            "bootstrapTotalMaxChars": 100_000,
            "bootstrapMaxChars": 40_000,
        })
        gateway.update({
            "mode": "local",
            "bind": "loopback",
            "auth": {"mode": "token", "token": "0" * 64},
            "trustedProxies": [],
        })
        cfg["tools"] = {
            # Member-bot default since the 2026-05-25 pivot (see
            # docs/spec-app-derived-permissions-2026-05-24.md). Tests that
            # want the deny path should set ``execPolicy: "deny"`` in the
            # network fixture to force it.
            "exec": {"security": "full", "ask": "on-miss"},
            "web": {
                "search": {"enabled": True},
                "fetch": {"enabled": True},
            },
        }
        cfg["commands"] = {"native": "auto", "nativeSkills": "auto"}
        # NB: no top-level "sandbox" — OC schema rejects it.
        cfg["session"] = {
            "dmScope": "per-channel-peer",
            "reset": {"idleMinutes": 120},
        }
        # ensure_plugin_config gap-fills logging.file/maxFileBytes so OC's
        # logger rotates instead of falling through to launchd's stdout
        # capture (which has no rotation). Fixture's default bot_user is
        # "admin_bot" via _network(); _bot_user_for() picks that up.
        cfg["logging"] = {
            "file": "/Users/admin_bot/.openclaw/logs/openclaw.log",
            "maxFileBytes": 26214400,
        }
    return json.dumps(cfg, indent=2)


def _network(bot_id: str = "admin_bot", role: str = "member") -> dict:
    return {
        "networkId": "my-pod",
        "sharedDir": "/Users/Shared/evolve",
        "bots": {bot_id: {"role": role, "port": 3000}},
    }


# ── ensure_plugin_config ──────────────────────────────────────────────────────

class TestEnsurePluginConfig:
    """ensure_plugin_config injects evolve entry when absent or stale."""

    def _run_with_json(
        self,
        oc_json_content: str,
        network: dict,
        bot_id: str = "admin_bot",
        ea_content: str | None = None,
    ) -> None:
        """Patch subprocess.run + Path.exists + Path.read_text so ensure_plugin_config can run.

        ``ea_content``: JSON string to return for exec-approvals.json reads.
        ``None`` (default) means the file is absent → inferred exec policy is
        ``"deny"`` (no allowlist entries). Pass a JSON string with
        ``agents.<id>.allowlist`` entries to get ``"allowlist"`` mode.
        """
        _ea = ea_content  # capture for closure

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            if any("cat" in part for part in cmd):
                if any("exec-approvals" in part for part in cmd):
                    if _ea is not None:
                        result.stdout = _ea
                    else:
                        result.returncode = 1
                        result.stderr = "No such file or directory"
                else:
                    result.stdout = oc_json_content
            elif "openclaw" in cmd and "config" in cmd and "validate" in cmd:
                result.stdout = json.dumps({"valid": True})
            return result

        def fake_read_text(*args, **kwargs):
            # Path.read_text is patched at class level; we can't inspect self
            # here, so return openclaw.json content. The exec-approvals read
            # path raises PermissionError (not FileNotFoundError) to trigger
            # the subprocess fallback handled by fake_run above.
            return oc_json_content

        # Phase 3 cutover (spec-openclaw-json-derived-artifact-2026-05-24):
        # ensure_plugin_config now delegates the evolve plugin config block
        # to ``openclaw_materializer.materialize_evolve_plugin_config``,
        # which reads ``read_bot_overrides`` and writes through
        # ``write_override``. The global Path.read_text patch above would
        # feed openclaw.json content into those reads (wrong shape →
        # empty BotOverrides). Pin the materializer's overrides I/O to an
        # in-memory store so the tests can isolate the gap-fill /
        # materialization semantics without standing up a real sandbox
        # directory.
        _in_memory_overrides: dict[str, dict] = {}

        def fake_read_bot_overrides(shared_dir, bot_id_param):
            from evolve_admin.config_sandbox.overrides import BotOverrides, OverrideEntry
            data = _in_memory_overrides.get(bot_id_param, {})
            entries = {
                k: OverrideEntry(
                    value=v["value"],
                    set_by=v["set_by"],
                    set_at=v["set_at"],
                    note=v.get("note"),
                    expires_at=v.get("expires_at"),
                    needs_review=v.get("needs_review", False),
                )
                for k, v in data.items()
            }
            return BotOverrides(bot_id=bot_id_param, overrides=entries)

        def fake_write_override(shared_dir, bot_id_param, key, value, *,
                                set_by, note=None, expires_at=None,
                                needs_review=False, now=None):
            from evolve_admin.config_sandbox.overrides import OverrideEntry
            _in_memory_overrides.setdefault(bot_id_param, {})[key] = {
                "value": value,
                "set_by": set_by,
                "set_at": "2026-05-25T00:00:00Z",
                "note": note,
                "expires_at": expires_at,
                "needs_review": needs_review,
            }
            return OverrideEntry(
                value=value, set_by=set_by, set_at="2026-05-25T00:00:00Z",
                note=note, expires_at=expires_at, needs_review=needs_review,
            )

        with (
            patch("evolve_admin.deploy.subprocess.run", side_effect=fake_run),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", side_effect=fake_read_text),
            patch("evolve_admin.deploy.safe_write_bot_config") as mock_write,
            patch("evolve_admin.openclaw_materializer.read_bot_overrides",
                  side_effect=fake_read_bot_overrides),
            patch("evolve_admin.openclaw_materializer.write_override",
                  side_effect=fake_write_override),
        ):
            mock_write.return_value = (True, "")
            ensure_plugin_config(bot_id, network)
            return mock_write

    def test_injects_config_when_absent(self):
        """Should write plugin config when plugins.entries.evolve is missing."""
        oc_json = _make_oc_json()  # no evolve entry
        net = _network()

        mock_write = self._run_with_json(oc_json, net)
        assert mock_write.called, "safe_write_bot_config should be called when config is absent"

        written_cfg = mock_write.call_args[0][1]
        evolve_entry = written_cfg["plugins"]["entries"]["evolve"]
        assert evolve_entry["enabled"] is True
        assert evolve_entry["config"]["botId"] == "admin_bot"
        assert evolve_entry["config"]["role"] == "member"
        assert evolve_entry["config"]["networkId"] == "my-pod"

    def test_skips_when_config_already_correct(self):
        """Should not write if evolve entry already has every expected key.

        Post-refactor, ensure_plugin_config gap-fills every registry default,
        sets identity, sets dashboardEnabled from role, and sets
        subagent.allowModelOverride and hooks.allowConversationAccess. A
        "complete" entry must therefore include all of those — anything
        missing triggers a write.
        """
        existing_evolve = {
            "evolve": {
                "enabled": True,
                "config": {
                    "botId": "admin_bot",
                    "role": "member",
                    "networkId": "my-pod",
                    "sharedDir": "/Users/Shared/evolve",
                    "classifierModel": "anthropic/claude-haiku-4-5",
                    "tierClassification": "session",
                    "tier": "full",
                    "summarizerMinTurns": 2,
                    "classifierKeywordConfidenceFloor": 0.80,
                    "costLedgerEnabled": True,
                    "dashboardEnabled": False,
                },
                "subagent": {"allowModelOverride": True},
                "hooks": {"allowConversationAccess": True},
            }
        }
        oc_json = _make_oc_json(extra_plugins=existing_evolve, with_cost_defaults=True)
        net = _network()

        mock_write = self._run_with_json(oc_json, net)
        assert not mock_write.called, "safe_write_bot_config should NOT be called when config is already correct"

    def test_reinjects_when_network_id_changed(self):
        """Should update config when networkId has changed."""
        existing_evolve = {
            "evolve": {
                "enabled": True,
                "config": {
                    "botId": "admin_bot",
                    "role": "member",
                    "networkId": "old-pod",  # stale
                },
            }
        }
        oc_json = _make_oc_json(extra_plugins=existing_evolve)
        net = _network()  # networkId = "my-pod"

        mock_write = self._run_with_json(oc_json, net)
        assert mock_write.called, "safe_write_bot_config should be called when networkId changed"

        written_cfg = mock_write.call_args[0][1]
        assert written_cfg["plugins"]["entries"]["evolve"]["config"]["networkId"] == "my-pod"

    def test_null_role_in_network_defaults_to_member(self):
        """A null role in network.json (e.g. personal_bot's corrupted entry) must not
        propagate into plugin config — ensure_plugin_config must write 'member'."""
        oc_json = _make_oc_json()
        net = {
            "networkId": "my-pod",
            "sharedDir": "/Users/Shared/evolve",
            "bots": {"personal_bot": {"role": None, "port": 19005}},
        }

        mock_write = self._run_with_json(oc_json, net, bot_id="personal_bot")
        assert mock_write.called
        evolve_cfg = mock_write.call_args[0][1]["plugins"]["entries"]["evolve"]["config"]
        assert evolve_cfg["role"] == "member", (
            "null role in network.json must fall back to 'member', not propagate as None"
        )

    def test_sets_primary_role_dashboard_enabled(self):
        """dashboardEnabled should be True for primary role."""
        oc_json = _make_oc_json()
        net = _network(bot_id="team_bot_a", role="primary")

        mock_write = self._run_with_json(oc_json, net, bot_id="team_bot_a")
        assert mock_write.called
        evolve_cfg = mock_write.call_args[0][1]["plugins"]["entries"]["evolve"]["config"]
        assert evolve_cfg["dashboardEnabled"] is True

    def test_member_role_dashboard_disabled(self):
        """dashboardEnabled should be False for member role."""
        oc_json = _make_oc_json()
        net = _network(bot_id="admin_bot", role="member")

        mock_write = self._run_with_json(oc_json, net, bot_id="admin_bot")
        assert mock_write.called
        evolve_cfg = mock_write.call_args[0][1]["plugins"]["entries"]["evolve"]["config"]
        assert evolve_cfg["dashboardEnabled"] is False

    def test_raises_when_oc_json_missing(self):
        """Should raise RuntimeError if openclaw.json doesn't exist.

        Production code path is direct Path.read_text() then sudo /bin/cat
        fallback. "Truly missing" is detected by sudo cat returning a
        "No such file" stderr.
        """
        def fake_run(cmd, **kwargs):
            r = MagicMock()
            if any("cat" in part for part in cmd):
                r.returncode = 1
                r.stdout = ""
                r.stderr = "cat: /Users/ghost/.openclaw/openclaw.json: No such file or directory"
            else:
                r.returncode = 0
                r.stdout = ""
                r.stderr = ""
            return r

        with (
            patch("evolve_admin.deploy.subprocess.run", side_effect=fake_run),
            patch.object(Path, "read_text", side_effect=FileNotFoundError),
        ):
            with pytest.raises(RuntimeError, match="not found"):
                ensure_plugin_config("ghost", _network(bot_id="ghost"))

    # ── Refactor coverage: registry-driven plugin-config flow ────────────────
    # These tests pin the post-refactor behaviour: a single declarative defaults
    # registry (`_PLUGIN_CONFIG_DEFAULTS`) drives both the fresh-install path
    # and the gap-fill path, replacing the prior rewrite-vs-loop split that
    # caused the PR #312 incident.

    def test_fresh_config_seeds_all_registry_fields(self):
        """Empty plugins.entries → every registry field is present after the call.

        Identity, behaviour, v2 cost-hygiene, dashboardEnabled, and
        subagent.allowModelOverride must ALL be present in a single pass.
        """
        from evolve_admin.deploy import _PLUGIN_CONFIG_DEFAULTS

        oc_json = _make_oc_json()  # no evolve entry at all
        net = _network(bot_id="admin_bot", role="member")

        mock_write = self._run_with_json(oc_json, net)
        assert mock_write.called

        evolve_entry = mock_write.call_args[0][1]["plugins"]["entries"]["evolve"]
        cfg = evolve_entry["config"]

        # Identity
        assert cfg["botId"] == "admin_bot"
        assert cfg["role"] == "member"
        assert cfg["networkId"] == "my-pod"
        assert cfg["sharedDir"] == "/Users/Shared/evolve"

        # All registry defaults
        for k, v in _PLUGIN_CONFIG_DEFAULTS.items():
            assert cfg.get(k) == v, f"registry field {k!r} not seeded"

        # Role-conditional
        assert cfg["dashboardEnabled"] is False  # member

        # subagent.allowModelOverride
        assert evolve_entry["subagent"]["allowModelOverride"] is True

    def test_stale_identity_rewrites_only_identity(self):
        """Wrong networkId → identity fields rewritten; tuned non-identity preserved."""
        existing_evolve = {
            "evolve": {
                "enabled": True,
                "config": {
                    "botId": "admin_bot",
                    "role": "member",
                    "networkId": "old-pod",  # stale
                    "sharedDir": "/Users/Shared/evolve",
                    # Manually tuned non-identity values that must be preserved
                    "classifierKeywordConfidenceFloor": 0.95,
                    "summarizerMinTurns": 5,
                    "classifierModel": "anthropic/claude-haiku-4-5",
                    "tierClassification": "session",
                    "costLedgerEnabled": True,
                    "dashboardEnabled": False,
                },
                "subagent": {"allowModelOverride": True},
            }
        }
        oc_json = _make_oc_json(extra_plugins=existing_evolve, with_cost_defaults=True)
        net = _network()  # networkId = "my-pod"

        mock_write = self._run_with_json(oc_json, net)
        assert mock_write.called

        cfg = mock_write.call_args[0][1]["plugins"]["entries"]["evolve"]["config"]
        # Identity rewritten
        assert cfg["networkId"] == "my-pod"
        # Tuned non-identity preserved
        assert cfg["classifierKeywordConfidenceFloor"] == 0.95
        assert cfg["summarizerMinTurns"] == 5

    def test_pr312_scenario_gap_fills_v2_fields(self):
        """Identity correct + missing v2 fields → v2 fields gap-filled.

        Reproduces the exact pre-PR-#312 state: identity-complete entry that
        passed the old "complete enough" predicate but was missing the v2
        cost-hygiene fields.
        """
        existing_evolve = {
            "evolve": {
                "enabled": True,
                "config": {
                    "botId": "admin_bot",
                    "role": "member",
                    "networkId": "my-pod",
                    "sharedDir": "/Users/Shared/evolve",
                    # NOTE: no v2 fields (summarizerMinTurns, etc.)
                    "classifierModel": "anthropic/claude-haiku-4-5",
                    "tierClassification": "session",
                    "dashboardEnabled": False,
                },
                "subagent": {"allowModelOverride": True},
            }
        }
        oc_json = _make_oc_json(extra_plugins=existing_evolve, with_cost_defaults=True)
        net = _network()

        mock_write = self._run_with_json(oc_json, net)
        assert mock_write.called, "v2 fields missing → must gap-fill"

        cfg = mock_write.call_args[0][1]["plugins"]["entries"]["evolve"]["config"]
        # v2 fields seeded from registry defaults
        assert cfg["summarizerMinTurns"] == 2
        assert cfg["classifierKeywordConfidenceFloor"] == 0.80
        assert cfg["costLedgerEnabled"] is True
        # Pre-existing fields untouched
        assert cfg["classifierModel"] == "anthropic/claude-haiku-4-5"
        assert cfg["dashboardEnabled"] is False

    def test_gap_fills_logging_block_when_missing(self):
        """openclaw.json with no logging block → ensure_plugin_config writes
        ``logging.file`` (pointing at the bot's logs dir) and ``maxFileBytes``.

        Without ``logging.file`` set, OC falls through to its default
        ``/tmp/openclaw/...`` path and console-only output. launchd's
        StandardOut/Err capture then takes everything into
        ``gateway.log``/``.err.log`` unbounded — the test pod hit 100M+
        per bot as of 2026-06.
        """
        oc_json = _make_oc_json()  # no logging block, no evolve entry either
        net = _network(bot_id="admin_bot", role="member")

        mock_write = self._run_with_json(oc_json, net)
        assert mock_write.called

        written_cfg = mock_write.call_args[0][1]
        log_cfg = written_cfg.get("logging") or {}
        assert log_cfg.get("file") == "/Users/admin_bot/.openclaw/logs/openclaw.log"
        assert log_cfg.get("maxFileBytes") == 26214400

    def test_gap_fills_logging_block_when_partial(self):
        """Pre-existing logging block missing maxFileBytes → only the missing
        field is filled; existing ``file`` is rewritten to the canonical path
        so a stale path (e.g. ``/tmp/openclaw/...``) gets corrected.
        """
        oc_dict = json.loads(_make_oc_json())
        oc_dict["logging"] = {"file": "/tmp/openclaw/openclaw.log"}
        oc_json = json.dumps(oc_dict)
        net = _network(bot_id="admin_bot", role="member")

        mock_write = self._run_with_json(oc_json, net)
        assert mock_write.called

        log_cfg = mock_write.call_args[0][1].get("logging") or {}
        assert log_cfg["file"] == "/Users/admin_bot/.openclaw/logs/openclaw.log"
        assert log_cfg["maxFileBytes"] == 26214400

    def test_manually_tuned_values_preserved(self):
        """Per-bot tuned value (e.g. 0.95 floor) must NOT be overwritten by gap-fill."""
        existing_evolve = {
            "evolve": {
                "enabled": True,
                "config": {
                    "botId": "admin_bot",
                    "role": "member",
                    "networkId": "my-pod",
                    "sharedDir": "/Users/Shared/evolve",
                    "classifierModel": "anthropic/claude-haiku-4-5",
                    "tierClassification": "session",
                    "tier": "full",
                    "summarizerMinTurns": 2,
                    "classifierKeywordConfidenceFloor": 0.95,  # tuned
                    "costLedgerEnabled": True,
                    "dashboardEnabled": False,
                },
                "subagent": {"allowModelOverride": True},
                "hooks": {"allowConversationAccess": True},
            }
        }
        oc_json = _make_oc_json(extra_plugins=existing_evolve, with_cost_defaults=True)
        net = _network()

        mock_write = self._run_with_json(oc_json, net)
        # Nothing should change → write not called
        assert not mock_write.called, \
            "tuned-but-complete config must be a no-op"

    def test_idempotent_on_second_call(self):
        """Calling twice produces identical config; second call skips the write."""
        oc_json = _make_oc_json()
        net = _network(bot_id="admin_bot", role="member")

        # First call seeds everything
        mock_write1 = self._run_with_json(oc_json, net)
        assert mock_write1.called
        seeded_cfg = mock_write1.call_args[0][1]
        seeded_oc_json = json.dumps(seeded_cfg, indent=2)

        # Second call against the just-written config → no change
        mock_write2 = self._run_with_json(seeded_oc_json, net)
        assert not mock_write2.called, \
            "ensure_plugin_config must be idempotent — second call writes nothing"

    def test_preserves_out_of_band_channel_blocks(self):
        """A deploy must NEVER drop a channel added outside the install wizard.

        M1-B4b (``evolve_admin.channel_provisioning``) lets an operator add a
        second messaging channel to an already-provisioned bot by merging a
        ``channels.<id>`` block into openclaw.json. That feature is a mirage
        unless the next deploy carries the block through — ``ensure_plugin_config``
        is the only deploy-path writer of openclaw.json, and it read-modify-
        writes the live file rather than regenerating it from a template.

        This pins that: seed a config with an extra channel block that no
        Evolve code path ever writes, run the deploy pass, and assert the
        block survives byte-for-byte. If someone ever adds a desired-state
        channel list (or a prune of unrecognised channel ids) to the deploy
        path, this test is the tripwire.
        """
        import copy as _copy

        cfg = json.loads(_make_oc_json())
        # Deliberately NOT a channel any Evolve writer touches, with an
        # operator-set field alongside it.
        extra = {
            "enabled": True,
            "dmPolicy": "pairing",
            "groupPolicy": "allowlist",
            "operatorOnly": "must-survive-deploy",
        }
        cfg["channels"] = {"placeholder-channel": _copy.deepcopy(extra)}
        net = _network()

        mock_write = self._run_with_json(json.dumps(cfg, indent=2), net)
        assert mock_write.called, (
            "fixture is incomplete — this test needs a write to inspect"
        )
        written = mock_write.call_args[0][1]
        assert written["channels"]["placeholder-channel"] == extra, (
            "deploy dropped or rewrote an out-of-band channel block — "
            "add-a-channel (M1-B4b) would silently un-do itself"
        )

    def test_forces_tools_exec_security_full_when_missing(self):
        """Member bots with no tools.exec config and no exec-approvals get
        security=full (the new member-bot default — pivoted 2026-05-25,
        docs/spec-app-derived-permissions-2026-05-24.md).

        A member bot runs in its own user account; the right default is
        "can do anything in its own shell" rather than "treat as hostile".
        Tightening to allowlist mode is an operator opt-in.
        """
        oc_json = _make_oc_json()  # no tools.exec block
        net = _network()

        mock_write = self._run_with_json(oc_json, net)  # ea_content=None → absent
        assert mock_write.called

        cfg = mock_write.call_args[0][1]
        assert cfg["tools"]["exec"]["security"] == "full"
        assert cfg["tools"]["exec"]["ask"] == "on-miss"

    def test_sets_allowlist_mode_when_exec_approvals_exist(self):
        """A bot with exec-approvals allowlist entries gets security=allowlist."""
        oc_json = _make_oc_json()
        net = _network()
        ea = json.dumps({
            "agents": {"main": {"allowlist": [{"pattern": "/usr/bin/python3"}]}}
        })

        mock_write = self._run_with_json(oc_json, net, ea_content=ea)
        assert mock_write.called

        cfg = mock_write.call_args[0][1]
        assert cfg["tools"]["exec"]["security"] == "allowlist"
        assert cfg["tools"]["exec"]["ask"] == "on-miss"

    def test_explicit_execpolicy_overrides_inference(self):
        """execPolicy in network.json takes priority over exec-approvals inference."""
        oc_json = _make_oc_json()
        # Network explicitly forces "deny" — overrides the new "full" default
        net = _network()
        net["bots"]["admin_bot"]["execPolicy"] = "deny"

        mock_write = self._run_with_json(oc_json, net)
        assert mock_write.called

        cfg = mock_write.call_args[0][1]
        assert cfg["tools"]["exec"]["security"] == "deny"
        assert "ask" not in cfg["tools"]["exec"]

    def test_corrects_deny_to_full_when_no_explicit_override(self):
        """A member bot stuck at security=deny (legacy default or the 2026-05-22
        OC-migrator reversion incident) gets corrected to full on the next
        deploy — restores exec access for app-declared scripts.

        See docs/spec-app-derived-permissions-2026-05-24.md §"Migration plan
        / Phase A": the deploy is the self-heal moment for team_bot_a/admin_bot/team_bot_b/
        personal_bot/team_bot_c after the 2026-05-24 Slack failure.
        """
        oc_json_dict = json.loads(_make_oc_json(with_cost_defaults=True))
        oc_json_dict["tools"]["exec"]["security"] = "deny"
        # When deny is set, "ask" is removed; restore for the test starting state
        oc_json_dict["tools"]["exec"].pop("ask", None)
        oc_json = json.dumps(oc_json_dict)
        net = _network()

        mock_write = self._run_with_json(oc_json, net)
        assert mock_write.called, (
            "ensure_plugin_config must heal the legacy deny default back to "
            "the new full member-bot default"
        )
        cfg = mock_write.call_args[0][1]
        assert cfg["tools"]["exec"]["security"] == "full"
        assert cfg["tools"]["exec"]["ask"] == "on-miss"

    def test_role_change_primary_to_member_flips_dashboard(self):
        """Role change primary → member flips dashboardEnabled to False."""
        existing_evolve = {
            "evolve": {
                "enabled": True,
                "config": {
                    "botId": "team_bot_a",
                    "role": "primary",  # stale role in config
                    "networkId": "my-pod",
                    "sharedDir": "/Users/Shared/evolve",
                    "classifierModel": "anthropic/claude-haiku-4-5",
                    "tierClassification": "session",
                    "summarizerMinTurns": 2,
                    "classifierKeywordConfidenceFloor": 0.80,
                    "costLedgerEnabled": True,
                    "dashboardEnabled": True,  # was primary
                },
                "subagent": {"allowModelOverride": True},
            }
        }
        oc_json = _make_oc_json(extra_plugins=existing_evolve, with_cost_defaults=True)
        # Network now says team_bot_a is a member (e.g. demoted)
        net = _network(bot_id="team_bot_a", role="member")

        mock_write = self._run_with_json(oc_json, net, bot_id="team_bot_a")
        assert mock_write.called

        cfg = mock_write.call_args[0][1]["plugins"]["entries"]["evolve"]["config"]
        assert cfg["role"] == "member"
        assert cfg["dashboardEnabled"] is False

    def test_loopback_bot_gets_empty_trusted_proxies(self):
        """Loopback-only bot with no trustedProxies key gets [] gap-filled.

        OC security audit fires gateway.trusted_proxies_missing on every
        deploy otherwise; cosmetic for loopback but noisy on the Alerts page.
        """
        oc_json = _make_oc_json()  # bind isn't set yet → defaults to loopback below
        net = _network()

        mock_write = self._run_with_json(oc_json, net)
        assert mock_write.called

        gw = mock_write.call_args[0][1]["gateway"]
        assert gw["bind"] == "loopback"
        assert gw["trustedProxies"] == []

    def test_existing_trusted_proxies_list_preserved(self):
        """An operator-configured list of proxy IPs must NOT be overwritten."""
        oc_json_dict = json.loads(_make_oc_json())
        oc_json_dict["gateway"]["bind"] = "loopback"
        oc_json_dict["gateway"]["trustedProxies"] = ["10.0.0.1", "10.0.0.2"]
        oc_json = json.dumps(oc_json_dict)
        net = _network()

        mock_write = self._run_with_json(oc_json, net)
        assert mock_write.called  # other gap-fills still fire on this fixture
        gw = mock_write.call_args[0][1]["gateway"]
        assert gw["trustedProxies"] == ["10.0.0.1", "10.0.0.2"], \
            "operator-set trustedProxies must be preserved"

    def _run_brave_gap_fill(
        self,
        oc_json: str,
        net: dict,
        pod_key: str | None,
        already_installed: bool = False,
    ):
        """Run ensure_plugin_config with the pod-keystore resolver stubbed.

        ``already_installed`` controls what the install-records check reports,
        which is what gates the entry/key write (we only touch the entry when
        an install record exists, else deploys oscillate entry-then-strip).

        Returns (mock_run, mock_write) so callers can assert on both the
        install attempt and the config that would be written.
        """
        with (
            patch("evolve_admin.deploy.subprocess.run") as mock_run,
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value=oc_json),
            patch("evolve_admin.deploy.safe_write_bot_config") as mock_write,
            patch(
                "evolve_admin.deploy.resolve_pod_brave_key",
                return_value=pod_key,
            ),
            patch(
                "evolve_admin.safe_upgrade._installed_plugin_ids",
                return_value=(["brave"] if already_installed else []),
            ),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=oc_json, stderr="")
            mock_write.return_value = (True, "")
            ensure_plugin_config("admin_bot", net)
        return mock_run, mock_write

    @staticmethod
    def _brave_install_attempted(mock_run) -> bool:
        return any(
            "openclaw" in (call.args[0] if call.args else [])
            and "plugins" in call.args[0]
            and "install" in call.args[0]
            and "@openclaw/brave-plugin" in call.args[0]
            for call in mock_run.call_args_list
        )

    def test_brave_install_gap_fill_when_key_resolvable(self):
        """With a resolvable key, a missing brave install self-heals.

        Preserves the #1260 gap-fill intent for the case that actually
        works: an existing bot whose brave install was wiped (e.g. an
        upstream API bump requiring a fresh per-bot install) repairs itself
        on the next deploy instead of needing a hand-run
        `sudo -u <bot> -H openclaw plugins install --force
        @openclaw/brave-plugin`.
        """
        oc_json = _make_oc_json()
        net = _network()

        mock_run, _ = self._run_brave_gap_fill(oc_json, net, pod_key="BSA-pod-key")

        assert self._brave_install_attempted(mock_run), (
            "ensure_plugin_config must attempt `openclaw plugins install "
            "@openclaw/brave-plugin` when brave is missing AND a key backs it"
        )

    def test_brave_not_installed_or_enabled_without_a_key(self):
        """No resolvable key → no install, and no `enabled: true`.

        Regression guard for the 2026-06-24 demotion fallout (#3219): this
        block kept force-installing brave and writing `enabled: true` with no
        key check, long after brave stopped being a pod invariant and after
        the plugin_monitor baseline dropped it. `enabled: true` is a
        capability claim that the Skills page, the plugin monitor, and the
        bots' own tool listings all read as "this works" — writing it
        without a key shipped a web_search tool that 401s at call time on
        6 of 9 mini bots and on VPS evo, with every surface reporting health.
        """
        oc_json = _make_oc_json()
        net = _network()

        mock_run, mock_write = self._run_brave_gap_fill(oc_json, net, pod_key=None)

        assert not self._brave_install_attempted(mock_run), (
            "brave must NOT be installed when no API key is resolvable"
        )
        if mock_write.called:
            entries = (
                mock_write.call_args[0][1]
                .get("plugins", {})
                .get("entries", {})
            )
            assert entries.get("brave", {}).get("enabled") is not True, (
                "brave must never be enabled without a key — enabled:true is a "
                "capability claim every status surface trusts"
            )

    def test_brave_pod_key_written_to_canonical_runtime_path(self):
        """A resolved pod key lands where the gateway actually reads it.

        The runtime path is plugins.entries.brave.config.webSearch.apiKey
        (see web/credentials_oc.brave_key_from_oc_config). Writing the key
        only to auth-profiles.json — which is what `evolve-admin keys sync`
        does — leaves the gateway with no key at all.
        """
        oc_json = _make_oc_json(extra_plugins={"brave": {"enabled": True}})
        net = _network()

        _, mock_write = self._run_brave_gap_fill(
            oc_json, net, pod_key="BSA-pod-key", already_installed=True,
        )

        assert mock_write.called
        brave_entry = (
            mock_write.call_args[0][1]["plugins"]["entries"]["brave"]
        )
        assert brave_entry["config"]["webSearch"]["apiKey"] == "BSA-pod-key"
        assert brave_entry["enabled"] is True

    # ── Reconcile unpinned install specs ─────────────────────────────────────
    # OC 2026.5.18+ fires `plugins.installs_unpinned_npm_specs` on bots whose
    # install records pre-date the auto-pin path. 2026-05-28 pod-wide deploy
    # tripped 7/8 bots; atlas was clean because it was set up post-auto-pin.

    def test_unpinned_install_records_get_repinned(self):
        """Each unpinned @openclaw/* install record gets re-installed with the
        already-resolved version, so the next OC audit sweep clears
        plugins.installs_unpinned_npm_specs.
        """
        oc_json = _make_oc_json()
        net = _network()

        # installs.json shape from a real bot: spec is bare, resolvedVersion
        # captures what's actually installed.
        installs_data = {
            "installRecords": {
                "brave": {
                    "source": "npm",
                    "spec": "@openclaw/brave-plugin",
                    "resolvedName": "@openclaw/brave-plugin",
                    "resolvedVersion": "2026.5.18",
                },
                "evolve": {
                    "source": "path",
                    "sourcePath": "/Users/Shared/evolve-plugin",
                },
                "slack": {
                    "source": "npm",
                    "spec": "@openclaw/slack@2026.5.22",  # already pinned
                    "resolvedName": "@openclaw/slack",
                    "resolvedVersion": "2026.5.22",
                },
            }
        }

        with (
            patch("evolve_admin.deploy.subprocess.run") as mock_run,
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value=oc_json),
            patch("evolve_admin.deploy.safe_write_bot_config") as mock_write,
            patch(
                "evolve_admin.safe_upgrade._read_installs_json",
                return_value=installs_data,
            ),
            patch(
                "evolve_admin.oc_neutralize.install_externalized_plugin",
                return_value=(True, ""),
            ) as mock_reinst,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=oc_json, stderr="")
            mock_write.return_value = (True, "")
            ensure_plugin_config("admin_bot", net)

        # brave (unpinned) must be re-installed with version pinned to its
        # resolvedVersion. Filter to npm_package='@openclaw/brave-plugin' since
        # the brave gap-fill helper also calls install_externalized_plugin
        # (without version) and we want to distinguish.
        repin_calls = [
            c for c in mock_reinst.call_args_list
            if c.kwargs.get("version") == "2026.5.18"
            and c.args[1] == "@openclaw/brave-plugin"
        ]
        assert repin_calls, (
            "expected install_externalized_plugin(user, '@openclaw/brave-plugin', "
            f"version='2026.5.18', ...) — got {mock_reinst.call_args_list!r}"
        )

        # slack is already pinned — must NOT be re-installed
        slack_repin_calls = [
            c for c in mock_reinst.call_args_list
            if c.args[1] == "@openclaw/slack"
        ]
        assert not slack_repin_calls, (
            "already-pinned slack must be skipped (idempotency); "
            f"got {slack_repin_calls!r}"
        )

        # evolve (path-source, no npm spec) must NOT be re-installed
        evolve_repin_calls = [
            c for c in mock_reinst.call_args_list
            if "evolve" in str(c.args[1])
        ]
        assert not evolve_repin_calls, (
            "path-source evolve record must be skipped (no spec to pin); "
            f"got {evolve_repin_calls!r}"
        )

    def test_unpinned_repin_skipped_when_resolved_version_absent(self):
        """If installs.json record has no resolvedVersion (broken install,
        partial write), skip re-installation rather than auto-pinning to OC
        version — that would risk a silent upgrade.
        """
        oc_json = _make_oc_json()
        net = _network()

        installs_data = {
            "installRecords": {
                "brave": {
                    "source": "npm",
                    "spec": "@openclaw/brave-plugin",
                    "resolvedName": "@openclaw/brave-plugin",
                    # no resolvedVersion
                },
            }
        }

        with (
            patch("evolve_admin.deploy.subprocess.run") as mock_run,
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value=oc_json),
            patch("evolve_admin.deploy.safe_write_bot_config") as mock_write,
            patch(
                "evolve_admin.safe_upgrade._read_installs_json",
                return_value=installs_data,
            ),
            patch(
                "evolve_admin.oc_neutralize.install_externalized_plugin",
                return_value=(True, ""),
            ) as mock_reinst,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=oc_json, stderr="")
            mock_write.return_value = (True, "")
            ensure_plugin_config("admin_bot", net)

        # The brave gap-fill (no version arg) may still fire if brave isn't
        # detected as installed — that's the OTHER gap-fill, with no version
        # kwarg. The RECONCILER must NOT have fired with a version arg.
        versioned_calls = [
            c for c in mock_reinst.call_args_list
            if c.kwargs.get("version") is not None
        ]
        assert not versioned_calls, (
            "reconciler must skip records missing resolvedVersion; "
            f"got {versioned_calls!r}"
        )

    # ── Strip-stale-fields pass ──────────────────────────────────────────────
    # Regression coverage for the PR #1525 incident: a field removed from
    # packages/plugin/openclaw.plugin.json::configSchema (which uses
    # additionalProperties: false) was never stripped from existing bots'
    # openclaw.json, so every `plugins install` afterwards failed with
    # "must NOT have additional properties" until the field was scrubbed by
    # hand. ensure_plugin_config now prunes any key absent from the manifest's
    # configSchema.properties — the canonical OC source of truth.

    def test_strips_stale_field_dropped_from_schema(self):
        """A field removed from configSchema is stripped from existing config.

        Reproduces the PR #1525 incident exactly: reportingEnabled lingers on
        the bot's openclaw.json after being deleted from the plugin schema,
        and the next ensure_plugin_config call must remove it so plugins
        install can succeed.
        """
        existing_evolve = {
            "evolve": {
                "enabled": True,
                "config": {
                    "botId": "admin_bot",
                    "role": "member",
                    "networkId": "my-pod",
                    "sharedDir": "/Users/Shared/evolve",
                    "classifierModel": "anthropic/claude-haiku-4-5",
                    "tierClassification": "session",
                    "tier": "full",
                    "summarizerMinTurns": 2,
                    "classifierKeywordConfidenceFloor": 0.80,
                    "costLedgerEnabled": True,
                    "dashboardEnabled": False,
                    "reportingEnabled": True,  # stale — not in current schema
                },
                "subagent": {"allowModelOverride": True},
                "hooks": {"allowConversationAccess": True},
            }
        }
        oc_json = _make_oc_json(extra_plugins=existing_evolve, with_cost_defaults=True)
        net = _network()

        mock_write = self._run_with_json(oc_json, net)
        assert mock_write.called, (
            "ensure_plugin_config must rewrite when a stale schema field is present"
        )

        cfg = mock_write.call_args[0][1]["plugins"]["entries"]["evolve"]["config"]
        assert "reportingEnabled" not in cfg, (
            "reportingEnabled was removed from configSchema and must be pruned"
        )
        # Live keys preserved
        assert cfg["botId"] == "admin_bot"
        assert cfg["tier"] == "full"
        assert cfg["costLedgerEnabled"] is True

    def test_strips_multiple_unknown_keys(self):
        """Every key absent from configSchema gets pruned in a single pass."""
        existing_evolve = {
            "evolve": {
                "enabled": True,
                "config": {
                    "botId": "admin_bot",
                    "role": "member",
                    "networkId": "my-pod",
                    "sharedDir": "/Users/Shared/evolve",
                    "classifierModel": "anthropic/claude-haiku-4-5",
                    "tierClassification": "session",
                    "tier": "full",
                    "summarizerMinTurns": 2,
                    "classifierKeywordConfidenceFloor": 0.80,
                    "costLedgerEnabled": True,
                    "dashboardEnabled": False,
                    # Hypothetical legacy keys an old config might still carry
                    "reportingEnabled": True,
                    "legacyDebugFlag": "verbose",
                    "experimentalKnob": 42,
                },
                "subagent": {"allowModelOverride": True},
                "hooks": {"allowConversationAccess": True},
            }
        }
        oc_json = _make_oc_json(extra_plugins=existing_evolve, with_cost_defaults=True)
        net = _network()

        mock_write = self._run_with_json(oc_json, net)
        assert mock_write.called

        cfg = mock_write.call_args[0][1]["plugins"]["entries"]["evolve"]["config"]
        for stale in ("reportingEnabled", "legacyDebugFlag", "experimentalKnob"):
            assert stale not in cfg, f"{stale} must be pruned"

    def test_schema_aligned_config_not_modified_by_strip(self):
        """A schema-clean config triggers no write — the strip pass is a no-op."""
        existing_evolve = {
            "evolve": {
                "enabled": True,
                "config": {
                    "botId": "admin_bot",
                    "role": "member",
                    "networkId": "my-pod",
                    "sharedDir": "/Users/Shared/evolve",
                    "classifierModel": "anthropic/claude-haiku-4-5",
                    "tierClassification": "session",
                    "tier": "full",
                    "summarizerMinTurns": 2,
                    "classifierKeywordConfidenceFloor": 0.80,
                    "costLedgerEnabled": True,
                    "dashboardEnabled": False,
                },
                "subagent": {"allowModelOverride": True},
                "hooks": {"allowConversationAccess": True},
            }
        }
        oc_json = _make_oc_json(extra_plugins=existing_evolve, with_cost_defaults=True)
        net = _network()

        mock_write = self._run_with_json(oc_json, net)
        assert not mock_write.called, (
            "fully schema-aligned config must be a no-op — strip pass shouldn't fire"
        )


# ── _allowed_plugin_config_keys ────────────────────────────────────────────────

class TestAllowedPluginConfigKeys:
    """The strip pass derives its allowed-key set from the plugin manifest's
    configSchema. Manifest-shape coverage prevents a malformed manifest from
    silently nuking every field, and a sanity check pins the schema's current
    surface so an accidental schema deletion shows up in code review."""

    def test_reads_known_keys_from_real_manifest(self):
        """Known schema fields are returned from the live manifest.

        Sanity check: if someone removes a load-bearing key from
        openclaw.plugin.json, this test fails fast — schema deletions then
        require an intentional update to this list rather than slipping
        through unnoticed (the PR #1525 failure mode).
        """
        from evolve_admin.deploy import _allowed_plugin_config_keys

        allowed = _allowed_plugin_config_keys()
        # Every field ensure_plugin_config writes must be in the schema —
        # otherwise the write itself would trigger the strip pass.
        expected_subset = {
            "botId", "role", "networkId", "sharedDir",
            "classifierModel", "tierClassification", "tier",
            "summarizerMinTurns", "classifierKeywordConfidenceFloor",
            "costLedgerEnabled", "dashboardEnabled",
        }
        missing = expected_subset - allowed
        assert not missing, (
            f"plugin manifest configSchema is missing expected keys: {missing}. "
            f"If you removed a key intentionally, also remove it from "
            f"_PLUGIN_CONFIG_DEFAULTS and any other writer in ensure_plugin_config."
        )

    def test_returns_empty_set_on_unreadable_manifest(self, tmp_path):
        """Unreadable manifest → empty set → strip pass skipped, not destructive.

        A broken manifest shouldn't silently nuke every field on every bot;
        far better to skip the prune and surface the schema breakage via
        plugins install instead. The allowed set now keys on the DEPLOYED
        (staged) manifest with a SOURCE fallback, so the empty-set contract
        holds only when NEITHER is readable — patch both dirs at the empty tmp.
        """
        from evolve_admin import deploy as _deploy

        with patch.object(_deploy, "PLUGIN_INSTALL_DIR", tmp_path), \
                patch.object(_deploy, "PLUGIN_SRC_DIR", tmp_path):
            assert _deploy._allowed_plugin_config_keys() == set()

    def test_returns_empty_set_on_malformed_manifest(self, tmp_path):
        """Manifest exists but is malformed JSON → empty set, no crash."""
        from evolve_admin import deploy as _deploy

        (tmp_path / "openclaw.plugin.json").write_text("not json at all {{{")
        with patch.object(_deploy, "PLUGIN_INSTALL_DIR", tmp_path), \
                patch.object(_deploy, "PLUGIN_SRC_DIR", tmp_path):
            assert _deploy._allowed_plugin_config_keys() == set()

    def test_returns_empty_set_when_properties_block_missing(self, tmp_path):
        """Valid JSON but configSchema.properties absent → empty set."""
        from evolve_admin import deploy as _deploy

        (tmp_path / "openclaw.plugin.json").write_text('{"id":"evolve"}')
        with patch.object(_deploy, "PLUGIN_INSTALL_DIR", tmp_path), \
                patch.object(_deploy, "PLUGIN_SRC_DIR", tmp_path):
            assert _deploy._allowed_plugin_config_keys() == set()


# ── repair_security_bot_config ──────────────────────────────────────────────────────

class TestRepairSecurityBotConfig:
    """repair_security_bot_config removes the evolve plugin entry from the security bot's config."""

    @pytest.fixture(autouse=True)
    def _allow_fabricated_dests(self):
        """See TestClearStalePluginInstall._allow_fabricated_dests — same reason:
        this class asserts on argv for a ``/Users/security_bot`` that does not
        exist on the test host, which the real-lstat D-2 gate refuses. The gate
        itself is pinned in test_deploy_sudo_dest_gate.py.
        """
        with patch("evolve_admin.deploy.sudo_dest_refusal", return_value=""):
            yield

    def _make_security_bot_json(self, with_evolve: bool, bot_user: str = "security_bot") -> str:
        cfg: dict = {
            "agents": {"defaults": {"workspace": f"/Users/{bot_user}/.openclaw/workspace"}},
            "gateway": {"port": 9999},
            "plugins": {"entries": {}},
        }
        if with_evolve:
            cfg["plugins"]["entries"]["evolve"] = {
                "enabled": True,
                "config": {"botId": bot_user, "role": "member", "networkId": "my-pod"},
            }
        return json.dumps(cfg, indent=2)

    def _write_network(
        self,
        tmp_path: Path,
        security_bot_id: str | None = "security_bot",
        bot_user_overrides: dict | None = None,
    ) -> Path:
        """Write a minimal network.json to tmp_path and return the path."""
        bots: dict = {}
        if security_bot_id:
            entry: dict = {"role": "member", "port": 9999}
            if bot_user_overrides and security_bot_id in bot_user_overrides:
                entry["user"] = bot_user_overrides[security_bot_id]
            bots[security_bot_id] = entry
        net = {
            "networkId": "test-pod",
            "members": list(bots.keys()),
            "bots": bots,
            "sharedDir": "/tmp/evolve-test-shared",
            "security": {"botId": security_bot_id} if security_bot_id else {},
        }
        net_path = tmp_path / "network.json"
        net_path.write_text(json.dumps(net))
        return net_path

    def _patch_env(
        self,
        home_exists: bool,
        oc_json_exists: bool,
        oc_json_content: str,
        bot_user: str = "security_bot",
    ):
        """Context manager: patch Path.exists and subprocess.run for repair tests."""
        import stat as _stat

        written: list[str] = []
        home_str = f"/Users/{bot_user}"
        oc_str = f"/Users/{bot_user}/.openclaw/openclaw.json"

        def fake_exists(self_path):
            if str(self_path) == home_str:
                return home_exists
            if str(self_path) == oc_str:
                return oc_json_exists
            return True  # temp files, network.json, etc.

        def fake_stat(self_path):
            s = MagicMock()
            s.st_mode = _stat.S_IFDIR | 0o755
            s.st_uid = 501
            s.st_gid = 20
            return s

        def fake_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if any("cat" in part for part in cmd):
                r.stdout = oc_json_content
            return r

        def fake_write_text(self_path, content):
            written.append(content)

        return (
            patch.object(Path, "exists", fake_exists),
            patch.object(Path, "stat", fake_stat),
            patch("evolve_admin.deploy.subprocess.run", side_effect=fake_run),
            patch.object(Path, "write_text", fake_write_text),
            patch.object(Path, "unlink", return_value=None),
            written,
        )

    def _patch_pwd_grp(self, user: str = "security_bot"):
        return (
            patch("evolve_admin.deploy.pwd.getpwuid", return_value=MagicMock(pw_name=user)),
            patch("evolve_admin.deploy.grp.getgrgid", return_value=MagicMock(gr_name="staff")),
        )

    def test_removes_evolve_entry_when_present(self, tmp_path):
        net_path = self._write_network(tmp_path)
        oc_json = self._make_security_bot_json(with_evolve=True)
        patches = self._patch_env(True, True, oc_json)
        written = patches[-1]
        ctx_managers = patches[:-1]
        pwd_p, grp_p = self._patch_pwd_grp("security_bot")

        with ctx_managers[0], ctx_managers[1], ctx_managers[2], ctx_managers[3], ctx_managers[4], pwd_p, grp_p:
            result = repair_security_bot_config(bot_id="security_bot", network_path=net_path)

        assert result["error"] is None
        assert result["changed"] is True
        assert result["bot_id"] == "security_bot"
        assert written, "should have written cleaned config"
        cleaned = json.loads(written[0])
        assert "evolve" not in cleaned["plugins"]["entries"]

    def test_no_change_when_evolve_absent(self, tmp_path):
        net_path = self._write_network(tmp_path)
        oc_json = self._make_security_bot_json(with_evolve=False)
        patches = self._patch_env(True, True, oc_json)
        ctx_managers = patches[:-1]
        pwd_p, grp_p = self._patch_pwd_grp("security_bot")

        with ctx_managers[0], ctx_managers[1], ctx_managers[2], ctx_managers[3], ctx_managers[4], pwd_p, grp_p:
            result = repair_security_bot_config(bot_id="security_bot", network_path=net_path)

        assert result["error"] is None
        assert result["changed"] is False

    def test_error_when_home_missing(self, tmp_path):
        net_path = self._write_network(tmp_path)
        patches = self._patch_env(False, False, "")
        ctx_managers = patches[:-1]

        with ctx_managers[0]:
            result = repair_security_bot_config(bot_id="security_bot", network_path=net_path)

        assert result["error"] is not None
        assert "does not exist" in result["error"]
        assert "security_bot" in result["error"]

    def test_warns_when_oc_json_missing(self, tmp_path):
        net_path = self._write_network(tmp_path)
        patches = self._patch_env(True, False, "")
        ctx_managers = patches[:-1]
        pwd_p, grp_p = self._patch_pwd_grp("security_bot")

        with ctx_managers[0], ctx_managers[1], ctx_managers[2], ctx_managers[3], ctx_managers[4], pwd_p, grp_p:
            result = repair_security_bot_config(bot_id="security_bot", network_path=net_path)

        assert result["error"] is None
        assert result["changed"] is False
        assert any("not found" in w for w in result["warnings"])

    # ── New tests: bot_id derivation and error paths ──────────────────────────

    def test_derives_bot_id_from_network_security(self, tmp_path):
        """When bot_id is None, derive it from network.security.botId."""
        net_path = self._write_network(tmp_path, security_bot_id="security_bot")
        oc_json = self._make_security_bot_json(with_evolve=True)
        patches = self._patch_env(True, True, oc_json)
        written = patches[-1]
        ctx_managers = patches[:-1]
        pwd_p, grp_p = self._patch_pwd_grp("security_bot")

        with ctx_managers[0], ctx_managers[1], ctx_managers[2], ctx_managers[3], ctx_managers[4], pwd_p, grp_p:
            result = repair_security_bot_config(network_path=net_path)  # no bot_id arg

        assert result["error"] is None
        assert result["bot_id"] == "security_bot"
        assert result["changed"] is True

    def test_uses_bot_user_override_from_network(self, tmp_path):
        """When network.bots[<id>].user is set, the file path should use that user."""
        net_path = self._write_network(
            tmp_path,
            security_bot_id="audit",
            bot_user_overrides={"audit": "alice"},
        )
        oc_json = self._make_security_bot_json(with_evolve=True, bot_user="alice")
        patches = self._patch_env(True, True, oc_json, bot_user="alice")
        written = patches[-1]
        ctx_managers = patches[:-1]
        pwd_p, grp_p = self._patch_pwd_grp("alice")

        with ctx_managers[0], ctx_managers[1], ctx_managers[2], ctx_managers[3], ctx_managers[4], pwd_p, grp_p:
            result = repair_security_bot_config(network_path=net_path)

        assert result["error"] is None
        assert result["bot_id"] == "audit"
        # The cleaned config was written via /tmp staging — confirm a write happened
        assert written, "should have written cleaned config under alice's home"

    def test_error_when_no_security_bot_configured(self, tmp_path):
        """When network.security.botId is unset and no bot_id passed, return clear error."""
        net_path = self._write_network(tmp_path, security_bot_id=None)

        result = repair_security_bot_config(network_path=net_path)

        assert result["error"] is not None
        assert "No security bot configured" in result["error"]
        assert "evolve-admin repair-security_bot --bot" in result["error"]
        assert result["bot_id"] is None
        assert result["changed"] is False

    def test_error_when_bot_home_does_not_exist(self, tmp_path):
        """When the resolved bot has no home directory, return a clear error mentioning the bot."""
        net_path = self._write_network(tmp_path, security_bot_id="ghostbot")
        patches = self._patch_env(False, False, "", bot_user="ghostbot")
        ctx_managers = patches[:-1]

        with ctx_managers[0]:
            result = repair_security_bot_config(network_path=net_path)

        assert result["error"] is not None
        assert "ghostbot" in result["error"]
        assert "does not exist" in result["error"]


# ── install_oc_plugin: re-injection after doctor --fix ───────────────────────

class TestInstallOcPluginReinjection:
    """install_oc_plugin should call ensure_plugin_config after doctor --fix."""

    def test_calls_ensure_plugin_config_after_plugins_install(self):
        """Doctor --fix moved to a nightly launchd job in PR #G — install_oc_plugin
        no longer invokes it inline. ensure_plugin_config is still called after
        `plugins install` to re-inject our config the install may have rewritten."""
        from evolve_admin.deploy import install_oc_plugin

        net = _network()
        call_order: list[str] = []

        def fake_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = json.dumps({"valid": True})
            r.stderr = ""
            if "doctor" in cmd:
                call_order.append("doctor")
            elif "plugins" in cmd and "install" in cmd:
                call_order.append("install")
            return r

        with (
            patch("evolve_admin.deploy.subprocess.run", side_effect=fake_run),
            patch("evolve_admin.deploy.run_cmd",
                  side_effect=lambda *a, **kw: call_order.append("install")),
            patch("evolve_admin.deploy.ensure_plugin_config") as mock_ensure,
        ):
            mock_ensure.side_effect = lambda *a, **kw: call_order.append("ensure")
            install_oc_plugin("admin_bot", port=3000, network=net)

        # Doctor must NOT run inline — it's the launchd path now.
        assert "doctor" not in call_order, (
            "doctor --fix moved to a launchd job; install_oc_plugin must not invoke it"
        )
        # ensure_plugin_config still runs after `plugins install` rewrites openclaw.json.
        assert "ensure" in call_order
        assert "install" in call_order
        assert call_order.index("ensure") > call_order.index("install"), \
            "ensure_plugin_config must be called AFTER plugins install"

    def test_skips_ensure_when_no_network(self):
        """When network=None, ensure_plugin_config should NOT be called."""
        from evolve_admin.deploy import install_oc_plugin

        def fake_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = json.dumps({"valid": True})
            r.stderr = ""
            return r

        with (
            patch("evolve_admin.deploy.subprocess.run", side_effect=fake_run),
            patch("evolve_admin.deploy.run_cmd", side_effect=lambda *a, **kw: None),
            patch("evolve_admin.deploy.ensure_plugin_config") as mock_ensure,
        ):
            install_oc_plugin("admin_bot", port=3000, network=None)
            mock_ensure.assert_not_called()


# ── install_oc_plugin: pre-flight version check ──────────────────────────────

class TestInstallOcPluginVersionPreflight:
    """Pre-flight CLI/gateway version-match check inside install_oc_plugin.

    A partial brew/auto-updater cycle can leave the CLI ahead of the running
    gateway daemon; the symptom was a 30+ min diagnosis on 2026-05-23. The
    pre-flight check fails fast with a clear, actionable error.
    """

    def _baseline_fake_run(self, recorder: list[str] | None = None):
        """Return a subprocess.run fake that succeeds for every command.

        ``recorder`` (optional) appends a tag for each recognised cmd so tests
        can assert ordering / "plugins install ran or didn't".
        """
        def fake_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = json.dumps({"valid": True})
            r.stderr = ""
            if recorder is not None:
                if "doctor" in cmd:
                    recorder.append("doctor")
                elif "plugins" in cmd and "install" in cmd:
                    recorder.append("install")
            return r
        return fake_run

    def test_aborts_when_cli_and_gateway_versions_differ(self):
        from evolve_admin.deploy import install_oc_plugin

        net = _network()
        recorder: list[str] = []
        with (
            patch("evolve_admin.deploy.subprocess.run", side_effect=self._baseline_fake_run(recorder)),
            patch("evolve_admin.deploy.run_cmd", side_effect=lambda *a, **kw: recorder.append("install")),
            patch("evolve_admin.deploy.ensure_plugin_config"),
            patch("evolve_admin.deploy._clear_stale_plugin_install"),
            patch("evolve_admin.deploy._read_oc_cli_version", return_value=("2026.5.22", "")),
            patch("evolve_admin.deploy._read_oc_gateway_version", return_value=("2026.5.20", "")),
        ):
            with pytest.raises(RuntimeError) as excinfo:
                install_oc_plugin("admin_bot", port=3000, network=net)

        msg = str(excinfo.value)
        assert "2026.5.22" in msg, "error must name the CLI version"
        assert "2026.5.20" in msg, "error must name the gateway version"
        assert "admin_bot" in msg, "error must name the bot"
        assert "kickstart" in msg, "error must tell the operator how to fix it"
        assert "ai.openclaw.admin_bot-gateway" in msg, "error must use the canonical launchctl label"
        # Critically — neither doctor --fix nor plugins install should have run
        assert "doctor" not in recorder, "doctor --fix must not run when versions mismatch"
        assert "install" not in recorder, "plugins install must not run when versions mismatch"

    def test_proceeds_when_versions_match(self):
        """When CLI and gateway versions match, preflight no-ops and install proceeds.
        Doctor --fix moved to a launchd job (PR #G) — no longer expected inline."""
        from evolve_admin.deploy import install_oc_plugin

        net = _network()
        recorder: list[str] = []
        with (
            patch("evolve_admin.deploy.subprocess.run", side_effect=self._baseline_fake_run(recorder)),
            patch("evolve_admin.deploy.run_cmd", side_effect=lambda *a, **kw: recorder.append("install")),
            patch("evolve_admin.deploy.ensure_plugin_config"),
            patch("evolve_admin.deploy._clear_stale_plugin_install"),
            patch("evolve_admin.deploy._read_oc_cli_version", return_value=("2026.5.22", "")),
            patch("evolve_admin.deploy._read_oc_gateway_version", return_value=("2026.5.22", "")),
        ):
            install_oc_plugin("admin_bot", port=3000, network=net)

        assert "doctor" not in recorder, "doctor --fix is now a launchd job, not inline"
        assert "install" in recorder, "plugins install should run when versions match"

    def test_skips_check_when_cli_version_unreadable(self):
        """Pre-flight check must not block deploys when its own readings fail."""
        from evolve_admin.deploy import install_oc_plugin

        net = _network()
        recorder: list[str] = []
        with (
            patch("evolve_admin.deploy.subprocess.run", side_effect=self._baseline_fake_run(recorder)),
            patch("evolve_admin.deploy.run_cmd", side_effect=lambda *a, **kw: recorder.append("install")),
            patch("evolve_admin.deploy.ensure_plugin_config"),
            patch("evolve_admin.deploy._clear_stale_plugin_install"),
            patch("evolve_admin.deploy._read_oc_cli_version", return_value=(None, "")),
            patch("evolve_admin.deploy._read_oc_gateway_version", return_value=("2026.5.22", "")),
        ):
            install_oc_plugin("admin_bot", port=3000, network=net)

        assert "install" in recorder, "deploy must proceed when CLI version is unreadable"

    def test_skips_check_when_gateway_unreachable(self):
        """Gateway not running = skip the check, don't block the deploy."""
        from evolve_admin.deploy import install_oc_plugin

        net = _network()
        recorder: list[str] = []
        with (
            patch("evolve_admin.deploy.subprocess.run", side_effect=self._baseline_fake_run(recorder)),
            patch("evolve_admin.deploy.run_cmd", side_effect=lambda *a, **kw: recorder.append("install")),
            patch("evolve_admin.deploy.ensure_plugin_config"),
            patch("evolve_admin.deploy._clear_stale_plugin_install"),
            patch("evolve_admin.deploy._read_oc_cli_version", return_value=("2026.5.22", "")),
            patch("evolve_admin.deploy._read_oc_gateway_version", return_value=(None, "")),
        ):
            install_oc_plugin("admin_bot", port=3000, network=net)

        assert "install" in recorder, "deploy must proceed when gateway version is unreadable"

    def test_raises_on_config_changed_stderr_from_cli(self):
        """The exact condition this preflight exists for: CLI errors with
        'config changed since last load' because it can't read its own config
        after a binary upgrade. Must escalate, not silently skip."""
        from evolve_admin.deploy import install_oc_plugin

        net = _network()
        recorder: list[str] = []
        drift_stderr = (
            "[openclaw] Could not start the CLI.\n"
            "[openclaw] Reason: config changed since last load\n"
        )
        with (
            patch("evolve_admin.deploy.subprocess.run", side_effect=self._baseline_fake_run(recorder)),
            patch("evolve_admin.deploy.run_cmd", side_effect=lambda *a, **kw: recorder.append("install")),
            patch("evolve_admin.deploy.ensure_plugin_config"),
            patch("evolve_admin.deploy._clear_stale_plugin_install"),
            patch("evolve_admin.deploy._read_oc_cli_version", return_value=(None, drift_stderr)),
            patch("evolve_admin.deploy._read_oc_gateway_version", return_value=(None, "")),
        ):
            with pytest.raises(RuntimeError) as excinfo:
                install_oc_plugin("admin_bot", port=3000, network=net)

        msg = str(excinfo.value)
        assert "config changed since last load" in msg, \
            "error must quote the upstream signature so operators can grep for it"
        assert "admin_bot" in msg, "error must name the bot"
        assert "kickstart" in msg, "error must tell the operator how to fix it"
        assert "ai.openclaw.admin_bot-gateway" in msg, \
            "error must use the canonical launchctl label"
        assert "doctor" not in recorder, "doctor --fix must not run after preflight raises"
        assert "install" not in recorder, "plugins install must not run after preflight raises"

    def test_raises_on_config_changed_stderr_from_gateway(self):
        """Same condition can surface from `gateway status --deep` instead of
        `--version`; either source must escalate."""
        from evolve_admin.deploy import install_oc_plugin

        net = _network()
        recorder: list[str] = []
        drift_stderr = "Error: config changed since last load"
        with (
            patch("evolve_admin.deploy.subprocess.run", side_effect=self._baseline_fake_run(recorder)),
            patch("evolve_admin.deploy.run_cmd", side_effect=lambda *a, **kw: recorder.append("install")),
            patch("evolve_admin.deploy.ensure_plugin_config"),
            patch("evolve_admin.deploy._clear_stale_plugin_install"),
            patch("evolve_admin.deploy._read_oc_cli_version", return_value=(None, "")),
            patch("evolve_admin.deploy._read_oc_gateway_version", return_value=(None, drift_stderr)),
        ):
            with pytest.raises(RuntimeError) as excinfo:
                install_oc_plugin("admin_bot", port=3000, network=net)

        msg = str(excinfo.value)
        assert "config changed since last load" in msg
        assert "kickstart" in msg
        assert "doctor" not in recorder
        assert "install" not in recorder

    def test_kickstart_runs_when_gateway_wedged(self):
        """Wedged gateway → preflight auto-kickstarts → install proceeds. Doctor
        --fix is no longer inline (PR #G — moved to nightly launchd)."""
        from evolve_admin.deploy import install_oc_plugin, _OC_GATEWAY_WEDGED_MARKER

        net = _network()
        recorder: list[str] = []
        with (
            patch("evolve_admin.deploy.subprocess.run", side_effect=self._baseline_fake_run(recorder)),
            patch("evolve_admin.deploy.run_cmd", side_effect=lambda *a, **kw: recorder.append("install")),
            patch("evolve_admin.deploy.ensure_plugin_config"),
            patch("evolve_admin.deploy._clear_stale_plugin_install"),
            patch("evolve_admin.deploy._read_oc_cli_version", return_value=("2026.5.22", "")),
            patch(
                "evolve_admin.deploy._read_oc_gateway_version",
                return_value=(None, _OC_GATEWAY_WEDGED_MARKER),
            ),
            patch(
                "evolve_admin.deploy._kickstart_gateway_and_wait",
                return_value=(True, "gateway back up at version 2026.5.22"),
            ) as mock_kickstart,
        ):
            install_oc_plugin("admin_bot", port=3000, network=net)

        mock_kickstart.assert_called_once(), "wedged gateway must trigger kickstart"
        assert "doctor" not in recorder, "doctor --fix is now a launchd job, not inline"
        assert "install" in recorder, "plugins install should run after kickstart succeeds"

    def test_raises_when_wedged_and_kickstart_fails(self):
        """Wedged gateway + failed kickstart = surface the underlying problem
        rather than let doctor --fix and plugins install both hang on the
        same dead daemon."""
        from evolve_admin.deploy import install_oc_plugin, _OC_GATEWAY_WEDGED_MARKER

        net = _network()
        recorder: list[str] = []
        with (
            patch("evolve_admin.deploy.subprocess.run", side_effect=self._baseline_fake_run(recorder)),
            patch("evolve_admin.deploy.run_cmd", side_effect=lambda *a, **kw: recorder.append("install")),
            patch("evolve_admin.deploy.ensure_plugin_config"),
            patch("evolve_admin.deploy._clear_stale_plugin_install"),
            patch("evolve_admin.deploy._read_oc_cli_version", return_value=("2026.5.22", "")),
            patch(
                "evolve_admin.deploy._read_oc_gateway_version",
                return_value=(None, _OC_GATEWAY_WEDGED_MARKER),
            ),
            patch(
                "evolve_admin.deploy._kickstart_gateway_and_wait",
                return_value=(False, "gateway did not respond within 30s after kickstart"),
            ),
        ):
            with pytest.raises(RuntimeError) as excinfo:
                install_oc_plugin("admin_bot", port=3000, network=net)

        msg = str(excinfo.value)
        assert "wedged" in msg, "error should name the cause so the operator can grep"
        assert "kickstart" in msg, "error must tell the operator how to fix it"
        assert "doctor" not in recorder, "doctor --fix must not run when kickstart failed"
        assert "install" not in recorder, "plugins install must not run when kickstart failed"


# ── install_oc_plugin: doctor --fix stderr surfacing ─────────────────────────

class TestInstallOcPluginDoctorRemoved:
    """PR #G moved doctor --fix out of install_oc_plugin's inline path —
    it's now a nightly per-bot launchd job + an on-demand
    ``evolve-admin doctor-pass`` CLI. The inline call was hitting 60s+
    timeouts on 6/8 bots during deploy and could never be reproduced
    manually with the same exact invocation. Pin the removal so a
    well-intentioned future refactor doesn't bring back the inline
    invocation without also addressing the original hang.
    """

    def test_install_oc_plugin_does_not_invoke_doctor(self):
        from evolve_admin.deploy import install_oc_plugin

        net = _network()
        recorder: list[str] = []

        def fake_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = json.dumps({"valid": True})
            r.stderr = ""
            if isinstance(cmd, list) and "doctor" in cmd:
                recorder.append("doctor")
            return r

        with (
            patch("evolve_admin.deploy.subprocess.run", side_effect=fake_run),
            patch("evolve_admin.deploy.run_cmd", side_effect=lambda *a, **kw: None),
            patch("evolve_admin.deploy.ensure_plugin_config"),
            patch("evolve_admin.deploy._clear_stale_plugin_install"),
            patch("evolve_admin.deploy._preflight_oc_version_match"),
        ):
            install_oc_plugin("admin_bot", port=3000, network=net)

        assert "doctor" not in recorder, (
            "install_oc_plugin must NOT invoke doctor --fix — it moved to "
            "a launchd job in PR #G (see _install_launchd_doctor_pass)."
        )


# ── _clear_stale_plugin_install ──────────────────────────────────────────────

class TestClearStalePluginInstall:
    """Force-clean drifted plugin installs before plugins install runs.

    Background: openclaw plugins install pre-validates the existing openclaw.json
    against the INSTALLED plugin's schema, not the source. When the source schema
    has gained fields since the bot's last successful install, ensure_plugin_config
    writes those fields and validation fails with "must NOT have additional
    properties". The plugin's package.json version isn't bumped per schema change,
    so we compare manifest file content directly.
    """

    @pytest.fixture(autouse=True)
    def _allow_fabricated_dests(self):
        """Neutralise the D-2 dest gate for this class's VIRTUAL filesystem.

        These tests fabricate ``/Users/<bot>/.openclaw/...`` via patched
        ``Path.exists``/``read_text``; the gate deliberately uses a real
        ``os.lstat`` (a patchable one would be no gate at all), so on the fake
        paths it correctly refuses with ENOENT. The gate's own behaviour —
        including that every refusal suppresses the cp AND the chown — is pinned
        against a REAL filesystem in test_deploy_sudo_dest_gate.py.
        """
        with patch("evolve_admin.deploy.sudo_dest_refusal", return_value=""):
            yield

    def _fake_fs(self, files: dict[str, str], dirs: set[str]) -> tuple:
        """Return (read_text_fake, exists_fake) bound to a virtual filesystem.

        ``files`` maps absolute path -> file contents.
        ``dirs`` is the set of directories that exist.
        """
        def read_text_fake(self_path, *args, **kwargs):
            key = str(self_path)
            if key in files:
                return files[key]
            raise FileNotFoundError(key)

        def exists_fake(self_path):
            key = str(self_path)
            return key in files or key in dirs

        return read_text_fake, exists_fake

    def test_noop_when_manifests_match(self):
        """Installed manifest matches source — no rm, no openclaw.json rewrite."""
        from evolve_admin.deploy import _clear_stale_plugin_install, PLUGIN_INSTALL_DIR

        manifest = json.dumps({"id": "evolve", "configSchema": {"properties": {"botId": {}}}})
        oc_cfg = json.dumps({"plugins": {"installs": {"evolve": {"version": "0.1.0"}}}})

        files = {
            str(PLUGIN_INSTALL_DIR / "openclaw.plugin.json"): manifest,
            "/Users/admin_bot/.openclaw/extensions/evolve/openclaw.plugin.json": manifest,
            "/Users/admin_bot/.openclaw/openclaw.json": oc_cfg,
        }
        dirs = {"/Users/admin_bot/.openclaw/extensions/evolve"}
        read_text_fake, exists_fake = self._fake_fs(files, dirs)

        run_calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            run_calls.append(list(cmd))
            r = MagicMock()
            r.returncode = 0
            return r

        with (
            patch.object(Path, "read_text", read_text_fake),
            patch.object(Path, "exists", exists_fake),
            patch("evolve_admin.deploy.subprocess.run", side_effect=fake_run),
        ):
            _clear_stale_plugin_install("admin_bot", "admin_bot")

        # No subprocess calls — nothing was stale.
        assert run_calls == [], f"expected no subprocess calls, got {run_calls}"

    def test_cleans_when_manifests_differ(self):
        """Installed manifest differs from source — rm install dir AND strip registry entry."""
        from evolve_admin.deploy import _clear_stale_plugin_install, PLUGIN_INSTALL_DIR

        src_manifest = json.dumps({
            "id": "evolve",
            "configSchema": {"properties": {"botId": {}, "newField": {}}},
        })
        old_manifest = json.dumps({
            "id": "evolve",
            "configSchema": {"properties": {"botId": {}}},
        })
        oc_cfg = json.dumps({
            "plugins": {
                "entries": {"evolve": {"enabled": True}},
                "installs": {"evolve": {"version": "0.1.0"}},
            },
        })

        files = {
            str(PLUGIN_INSTALL_DIR / "openclaw.plugin.json"): src_manifest,
            "/Users/personal_bot_user/.openclaw/extensions/evolve/openclaw.plugin.json": old_manifest,
            "/Users/personal_bot_user/.openclaw/openclaw.json": oc_cfg,
        }
        dirs = {"/Users/personal_bot_user/.openclaw/extensions/evolve"}
        read_text_fake, exists_fake = self._fake_fs(files, dirs)

        run_calls: list[list[str]] = []
        written_cfg: dict = {}

        def fake_run(cmd, **kwargs):
            run_calls.append(list(cmd))
            # Capture the openclaw.json that gets cp'd from /tmp
            if len(cmd) >= 4 and cmd[1] == "/bin/cp" and cmd[2].startswith("/tmp/"):
                try:
                    written_cfg.update(json.loads(Path(cmd[2]).read_text()))
                except Exception:
                    pass
            r = MagicMock()
            r.returncode = 0
            return r

        with (
            patch.object(Path, "read_text", read_text_fake),
            patch.object(Path, "exists", exists_fake),
            patch.object(Path, "write_text", lambda self, content: files.update(
                {str(self): content})),
            patch.object(Path, "unlink", lambda self, missing_ok=False: None),
            patch("evolve_admin.deploy.subprocess.run", side_effect=fake_run),
        ):
            _clear_stale_plugin_install("team_bot_b", "personal_bot_user")

        # Should have rm -rf'd the install dir
        rm_calls = [c for c in run_calls if "rm" in c[1]]
        assert len(rm_calls) == 1, f"expected one rm call, got {rm_calls}"
        assert "/Users/personal_bot_user/.openclaw/extensions/evolve" in rm_calls[0]

        # Should have cp'd a new openclaw.json
        cp_calls = [c for c in run_calls if "cp" in c[1]]
        assert any("/Users/personal_bot_user/.openclaw/openclaw.json" in c[-1] for c in cp_calls)

        # The written config must NOT have plugins.installs.evolve anymore
        assert "evolve" not in written_cfg.get("plugins", {}).get("installs", {})

    def test_strips_registry_when_install_dir_missing(self):
        """Install dir gone but registry still claims evolve installed — strip the entry."""
        from evolve_admin.deploy import _clear_stale_plugin_install, PLUGIN_INSTALL_DIR

        src_manifest = json.dumps({"id": "evolve"})
        oc_cfg = json.dumps({
            "plugins": {"installs": {"evolve": {"version": "0.1.0"}}},
        })

        files = {
            str(PLUGIN_INSTALL_DIR / "openclaw.plugin.json"): src_manifest,
            "/Users/ghost/.openclaw/openclaw.json": oc_cfg,
        }
        # extensions/evolve dir missing
        dirs: set[str] = set()
        read_text_fake, exists_fake = self._fake_fs(files, dirs)

        run_calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            run_calls.append(list(cmd))
            r = MagicMock()
            r.returncode = 0
            return r

        with (
            patch.object(Path, "read_text", read_text_fake),
            patch.object(Path, "exists", exists_fake),
            patch.object(Path, "write_text", lambda self, content: files.update(
                {str(self): content})),
            patch.object(Path, "unlink", lambda self, missing_ok=False: None),
            patch("evolve_admin.deploy.subprocess.run", side_effect=fake_run),
        ):
            _clear_stale_plugin_install("ghost", "ghost")

        # No rm (install dir doesn't exist); only cp + chown for the registry rewrite.
        rm_calls = [c for c in run_calls if "rm" in c[1]]
        assert rm_calls == [], f"expected no rm, got {rm_calls}"
        cp_calls = [c for c in run_calls if "cp" in c[1]]
        assert len(cp_calls) == 1

    def test_noop_when_nothing_installed(self):
        """First-ever install: no install dir, no registry entry. No-op."""
        from evolve_admin.deploy import _clear_stale_plugin_install, PLUGIN_INSTALL_DIR

        src_manifest = json.dumps({"id": "evolve"})
        oc_cfg = json.dumps({"plugins": {"entries": {}}})  # no installs

        files = {
            str(PLUGIN_INSTALL_DIR / "openclaw.plugin.json"): src_manifest,
            "/Users/fresh/.openclaw/openclaw.json": oc_cfg,
        }
        dirs: set[str] = set()
        read_text_fake, exists_fake = self._fake_fs(files, dirs)

        run_calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            run_calls.append(list(cmd))
            r = MagicMock()
            r.returncode = 0
            return r

        with (
            patch.object(Path, "read_text", read_text_fake),
            patch.object(Path, "exists", exists_fake),
            patch("evolve_admin.deploy.subprocess.run", side_effect=fake_run),
        ):
            _clear_stale_plugin_install("fresh", "fresh")

        assert run_calls == [], f"expected no-op, got {run_calls}"


# ── install_oc_plugin: signed-bypass gate ─────────────────────────────────────


class TestInstallOcPluginSignedBypass:
    """Spec: docs/spec-plugin-install-trust-2026-06-06.md §4.

    install_oc_plugin must call ``verify_plugin_signature(PLUGIN_INSTALL_DIR)``
    before installing, and refuse-to-install on mismatch. Verification is a
    precondition of installing at all — not of passing any OC flag. This
    closes PR #2293's blanket-bypass gap.

    These tests override the autouse stub (``_stub_plugin_signature``) by
    re-patching ``verify_plugin_signature`` with the specific behavior under
    test.
    """

    def test_refuses_install_when_signature_mismatches(self):
        from evolve_admin.deploy import install_oc_plugin

        net = _network()
        run_cmd_called = False

        def fake_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = json.dumps({"valid": True})
            r.stderr = ""
            return r

        def fake_run_cmd(*a, **kw):
            nonlocal run_cmd_called
            run_cmd_called = True

        with (
            patch("evolve_admin.deploy.verify_plugin_signature",
                  return_value=(False, "digest mismatch: stamped=sha256:a "
                                       "computed=sha256:b")),
            patch("evolve_admin.deploy.subprocess.run", side_effect=fake_run),
            patch("evolve_admin.deploy.run_cmd", side_effect=fake_run_cmd),
            patch("evolve_admin.deploy.ensure_plugin_config"),
            patch("evolve_admin.deploy._clear_stale_plugin_install"),
            patch("evolve_admin.deploy._preflight_oc_version_match"),
        ):
            with pytest.raises(RuntimeError) as excinfo:
                install_oc_plugin("admin_bot", port=3000, network=net)

        # The install command never ran — we failed closed before sudo.
        assert not run_cmd_called, (
            "signature mismatch must abort BEFORE the OC plugin install "
            "command runs"
        )
        # Error message names the bot, the cause, and the recovery.
        err = str(excinfo.value)
        assert "admin_bot" in err
        assert "signature verification failed" in err
        assert "digest mismatch" in err
        assert "evolve-admin upgrade" in err  # recovery path

    def test_proceeds_with_bypass_when_signature_matches(self):
        from evolve_admin.deploy import install_oc_plugin

        net = _network()
        install_cmd: list[str] = []

        def fake_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = json.dumps({"valid": True})
            r.stderr = ""
            return r

        def fake_run_cmd(cmd, **kw):
            install_cmd.extend(cmd)

        with (
            patch("evolve_admin.deploy.verify_plugin_signature",
                  return_value=(True, "")),
            patch("evolve_admin.deploy.subprocess.run", side_effect=fake_run),
            patch("evolve_admin.deploy.run_cmd", side_effect=fake_run_cmd),
            patch("evolve_admin.deploy.ensure_plugin_config"),
            patch("evolve_admin.deploy._clear_stale_plugin_install"),
            patch("evolve_admin.deploy._preflight_oc_version_match"),
        ):
            install_oc_plugin("admin_bot", port=3000, network=net)

        # The install proceeded — verification cleared.
        assert "plugins" in install_cmd and "install" in install_cmd
        assert "-l" in install_cmd

        # And it proceeded WITHOUT the deprecated bypass flag. OC 2026.7
        # deleted the install-time scanner: the runtime registers the flag as
        # "Deprecated no-op" and threads dangerouslyForceUnsafeInstall through
        # the whole install call chain without ever reading it (verified
        # against the installed 2026.7.1-2 runtime — every occurrence in
        # install-YXjfuIuN.js is a forward, zero in the scan/policy modules).
        # Passing it only earns a deprecation warning on every install and
        # invites the misreading that the digest gate exists to guard it.
        # The gate stands on its own — see the refuse-on-mismatch tests below.
        assert "--dangerously-force-unsafe-install" not in install_cmd, (
            "the deprecated bypass flag must not be passed — it is a no-op at "
            "the runtime, and coupling the content gate to it was never a real "
            "enforcement action (spec §4 'Mechanism correction')"
        )

    def test_refuses_when_manifest_unstamped(self):
        """A plugin built before the signed-bypass code lands (or with a
        manifest that lost its trust block) presents as 'not stamped'. Refuse
        with the same recovery message — rebuilding will stamp it."""
        from evolve_admin.deploy import install_oc_plugin

        net = _network()
        run_cmd_called = False

        def fake_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = json.dumps({"valid": True})
            r.stderr = ""
            return r

        def fake_run_cmd(*a, **kw):
            nonlocal run_cmd_called
            run_cmd_called = True

        with (
            patch("evolve_admin.deploy.verify_plugin_signature",
                  return_value=(False, "manifest is not stamped "
                                       "(missing x-evolve-trust.distDigest); "
                                       "rebuild the plugin to stamp it")),
            patch("evolve_admin.deploy.subprocess.run", side_effect=fake_run),
            patch("evolve_admin.deploy.run_cmd", side_effect=fake_run_cmd),
            patch("evolve_admin.deploy.ensure_plugin_config"),
            patch("evolve_admin.deploy._clear_stale_plugin_install"),
            patch("evolve_admin.deploy._preflight_oc_version_match"),
        ):
            with pytest.raises(RuntimeError) as excinfo:
                install_oc_plugin("admin_bot", port=3000, network=net)

        assert not run_cmd_called
        assert "not stamped" in str(excinfo.value)
