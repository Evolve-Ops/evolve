"""Phase 1 record_intent_on_set wiring on deploy.py's exec-policy seam.

Spec: docs/spec-config-intent-system-2026-05-21.md §3 (write-path
integration).

The flow this tests: deploy.ensure_plugin_config infers the bot's
exec posture via _infer_exec_policy and writes the result into
openclaw.json. When the inferred value diverges from the post-
2026-05-25 deploy baseline ("full" + "on-miss"), the deploy code now
records a config_intent so the next permission_monitor sweep
(intent-aware after PR #2295) does NOT fire a noisy perm_config_drift
signal for the deliberate deviation.

These tests exercise the new ``_record_exec_policy_intent`` helper
and ``_explain_exec_policy_inference`` separately from the larger
``ensure_plugin_config`` flow because the latter has too many
unrelated side effects (cost gap-fills, plugin entry injection, etc.)
to mock through cleanly. The helper-level tests cover all four
inference branches (network.json explicit, agent allowlist, defaults
allowlist, member-bot default) and the value-change idempotency
guard.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import deploy  # noqa: E402
from evolve_admin.config_intent import get_intent  # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    s = tmp_path / "shared"
    s.mkdir()
    return s


@pytest.fixture
def network(shared_dir: Path) -> dict:
    return {"sharedDir": str(shared_dir), "bots": {}}


# ── _explain_exec_policy_inference ───────────────────────────────────────────


class TestExplainExecPolicyInference:
    """The labels recorded on intents must distinguish the inference
    branch so future audits can see WHY a bot ended up with a non-
    baseline value without re-deriving from raw inputs.
    """

    def test_network_json_explicit_override(self):
        detail, reason = deploy._explain_exec_policy_inference(
            bot_cfg={"execPolicy": "deny"},
            exec_approvals=None,
            chosen="deny",
        )
        assert detail == "network.json:execPolicy"
        assert "execPolicy='deny'" in reason

    def test_agent_allowlist_branch(self):
        detail, reason = deploy._explain_exec_policy_inference(
            bot_cfg={},
            exec_approvals={
                "agents": {
                    "main": {"allowlist": ["ls", "cat"]},
                },
            },
            chosen="allowlist",
        )
        assert detail == "exec_approvals:agent_allowlist"
        assert "agent allowlist" in reason

    def test_defaults_allowlist_branch(self):
        detail, reason = deploy._explain_exec_policy_inference(
            bot_cfg={},
            exec_approvals={
                "defaults": {"allowlist": ["ls"]},
            },
            chosen="allowlist",
        )
        assert detail == "exec_approvals:defaults_allowlist"
        assert "defaults block" in reason

    def test_defaults_with_security_field_only_does_not_count(self):
        """exec-approvals.json defaults.security is the unix-socket auth
        posture for the exec-approvals daemon, NOT an allowlist. Mirrors
        the diagnosis doc fix that narrowed _infer_exec_policy priority 3.
        Falls through to the member-bot default branch."""
        detail, _ = deploy._explain_exec_policy_inference(
            bot_cfg={},
            exec_approvals={"defaults": {"security": "full"}},
            chosen="full",
        )
        assert detail == "member_bot_default"

    def test_member_bot_default_branch(self):
        detail, reason = deploy._explain_exec_policy_inference(
            bot_cfg={}, exec_approvals=None, chosen="full",
        )
        assert detail == "member_bot_default"
        assert "post-2026-05-25" in reason

    def test_empty_exec_approvals_falls_through_to_default(self):
        detail, _ = deploy._explain_exec_policy_inference(
            bot_cfg={}, exec_approvals={}, chosen="full",
        )
        assert detail == "member_bot_default"


# ── _record_exec_policy_intent — recording behavior ─────────────────────────


class TestRecordExecPolicyIntent:
    """The recorder must:
      - Record an intent for non-baseline values.
      - Skip recording when the value matches the deploy baseline.
      - Skip recording when an identical intent already exists (no
        redundant ``updated`` audit entries on every re-deploy).
      - Record the matching ``tools.exec.ask`` deviation when
        security="deny" causes the field to be removed.
    """

    def test_baseline_value_does_not_record_intent(self, shared_dir, network):
        """The most common case: member bot inherits exec=full as the
        2026-05-25 default. No intent should be written — the absence
        of an intent IS what "matches baseline" means."""
        deploy._record_exec_policy_intent(
            bot_id="team-bot-a",
            bot_cfg={},
            exec_approvals=None,
            security="full",
            network=network,
        )
        assert get_intent("team-bot-a", "tools.exec.security",
                          shared_dir=shared_dir) is None

    def test_deny_security_records_intent_with_explicit_override_detail(
        self, shared_dir, network,
    ):
        """Operator-set network.json execPolicy=deny → intent recorded
        with the network.json branch label so audits see the operator
        is the source."""
        deploy._record_exec_policy_intent(
            bot_id="team-bot-a",
            bot_cfg={"execPolicy": "deny"},
            exec_approvals=None,
            security="deny",
            network=network,
        )
        intent = get_intent("team-bot-a", "tools.exec.security",
                            shared_dir=shared_dir)
        assert intent is not None
        assert intent["value"] == "deny"
        assert intent["set_by"] == "deploy:exec_policy_inference"
        assert intent["set_by_detail"] == "network.json:execPolicy"

    def test_allowlist_security_records_intent_with_allowlist_detail(
        self, shared_dir, network,
    ):
        deploy._record_exec_policy_intent(
            bot_id="team-bot-a",
            bot_cfg={},
            exec_approvals={
                "agents": {"main": {"allowlist": ["ls", "cat"]}},
            },
            security="allowlist",
            network=network,
        )
        intent = get_intent("team-bot-a", "tools.exec.security",
                            shared_dir=shared_dir)
        assert intent is not None
        assert intent["value"] == "allowlist"
        assert intent["set_by_detail"] == "exec_approvals:agent_allowlist"

    def test_deny_security_also_records_ask_removal(self, shared_dir, network):
        """When security=deny, _infer_exec_policy / deploy code deletes
        tools.exec.ask entirely. The deploy baseline for ask is "on-miss";
        the post-write field is absent (== None in dotted-path land).
        Both deviations get recorded so the monitor can resolve both
        diffs via intent rather than flagging them as drift."""
        deploy._record_exec_policy_intent(
            bot_id="team-bot-a",
            bot_cfg={"execPolicy": "deny"},
            exec_approvals=None,
            security="deny",
            network=network,
        )
        ask_intent = get_intent("team-bot-a", "tools.exec.ask",
                                shared_dir=shared_dir)
        assert ask_intent is not None
        assert ask_intent["value"] is None
        assert "removed because exec security is 'deny'" in ask_intent["reason"]

    def test_idempotent_on_existing_matching_intent(self, shared_dir, network,
                                                     monkeypatch):
        """Re-deploy with the same target value should NOT add an
        ``updated`` history entry — set_intent is last-write-wins but
        we guard with a get_intent check so audit_history stays clean.
        """
        # Seed an existing intent matching the value the recorder would
        # write.
        deploy._record_exec_policy_intent(
            bot_id="team-bot-a",
            bot_cfg={"execPolicy": "deny"},
            exec_approvals=None,
            security="deny",
            network=network,
        )
        intent_before = get_intent("team-bot-a", "tools.exec.security",
                                    shared_dir=shared_dir)
        assert intent_before is not None
        history_len_before = len(intent_before["audit_history"])

        # Same call again — should be a no-op.
        deploy._record_exec_policy_intent(
            bot_id="team-bot-a",
            bot_cfg={"execPolicy": "deny"},
            exec_approvals=None,
            security="deny",
            network=network,
        )
        intent_after = get_intent("team-bot-a", "tools.exec.security",
                                   shared_dir=shared_dir)
        history_len_after = len(intent_after["audit_history"])
        assert history_len_after == history_len_before, (
            "Re-deploy with same value bloated audit_history; "
            "_record_exec_policy_intent's value-equality guard must "
            "prevent the no-op set_intent call"
        )

    def test_value_change_updates_existing_intent(self, shared_dir, network):
        """When the operator changes their mind — e.g., adds an
        execPolicy override after the bot was already running on
        member-bot default "full" — the recorder updates the existing
        intent's value and appends an audit history entry. set_intent
        already handles this; the recorder just needs to call through
        instead of guarding."""
        # First deploy: explicit deny override.
        deploy._record_exec_policy_intent(
            bot_id="team-bot-a",
            bot_cfg={"execPolicy": "deny"},
            exec_approvals=None,
            security="deny",
            network=network,
        )
        # Second deploy: operator removed the override and added an
        # exec-approvals allowlist → recorder updates the intent.
        deploy._record_exec_policy_intent(
            bot_id="team-bot-a",
            bot_cfg={},
            exec_approvals={
                "agents": {"main": {"allowlist": ["ls"]}},
            },
            security="allowlist",
            network=network,
        )
        intent = get_intent("team-bot-a", "tools.exec.security",
                            shared_dir=shared_dir)
        assert intent is not None
        assert intent["value"] == "allowlist"
        assert intent["set_by_detail"] == "exec_approvals:agent_allowlist"
        # Audit history shows both events.
        assert any(h["event"] == "updated" for h in intent["audit_history"])

    def test_import_failure_fails_open(self, shared_dir, network, monkeypatch):
        """If evolve_admin.config_intent isn't importable in the running
        environment (e.g. mid-rollout where deploy.py is new but the
        intent module isn't deployed yet), the recorder returns without
        raising so the deploy itself doesn't fail."""
        import builtins
        real_import = builtins.__import__

        def _block(name, *a, **kw):
            if name == "evolve_admin.config_intent":
                raise ImportError("simulated")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", _block)

        # Should not raise.
        deploy._record_exec_policy_intent(
            bot_id="team-bot-a",
            bot_cfg={"execPolicy": "deny"},
            exec_approvals=None,
            security="deny",
            network=network,
        )
        # And of course nothing was written.
        assert not (shared_dir / "config_intents").exists()
