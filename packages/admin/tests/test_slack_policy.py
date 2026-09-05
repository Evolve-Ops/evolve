"""Tests for evolve_admin.integrations.slack.policy + writer.

Schema round-trips, writer ownership boundary, drift detection, and
the synthesize-from-openclaw bootstrap. The writer's preservation
contract is the highest-risk area — a bug here clobbers telegram /
discord / hooks / agent config. Every preservation guarantee is
asserted explicitly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolve_admin.integrations.slack.policy import (
    AccessPolicy, ChannelsPolicy, DefaultForNew, DmAllowlist, MessagingPolicy,
    PolicyChannelEntry, SCHEMA_VERSION, SlackPolicy, WorkspaceMeta,
    load_policy, policy_path, save_policy, validate_policy,
)
from evolve_admin.integrations.slack.writer import (
    GROUP_POLICY, is_render_up_to_date, merge_policy_into_openclaw,
    synthesize_policy_from_openclaw,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────


def _minimal_policy(
    bot_id: str = "team_bot_a",
    *,
    channels: list[PolicyChannelEntry] | None = None,
    dm_mode: str = "open",
    user_ids: list[str] | None = None,
    visible_replies: str = "automatic",
) -> SlackPolicy:
    return SlackPolicy(
        bot_id=bot_id,
        workspace=WorkspaceMeta(team_id="T123", team_name="Test"),
        access=AccessPolicy(dm_allowlist=DmAllowlist(
            mode=dm_mode,  # type: ignore[arg-type]
            user_ids=user_ids or [],
        )),
        channels=ChannelsPolicy(entries=channels or []),
        messaging=MessagingPolicy(visible_replies_default=visible_replies),
    )


def _entry(cid: str, **kwargs) -> PolicyChannelEntry:
    return PolicyChannelEntry(channel_id=cid, **kwargs)


def _existing(
    *,
    bot_token: str = "xoxb-test",
    slack_extras: dict | None = None,
    channels: dict | None = None,
    other_providers: dict | None = None,
    visible_replies: str | None = "automatic",
    allow_from: list[str] | None = None,
) -> dict:
    """Build an openclaw.json existing-state dict matching the real OC shape.

    Channel allowlist goes under ``channels.slack.channels``; user
    allowlist (``allowFrom``) goes under ``channels.slack``. The shape
    is verified against team_bot_a + admin_bot on the production mini (2026-05-13).
    """
    slack: dict = {"botToken": bot_token}
    if slack_extras:
        slack.update(slack_extras)
    if channels is not None:
        slack["channels"] = dict(channels)
    if allow_from is not None:
        slack["allowFrom"] = list(allow_from)
    payload: dict = {"channels": {"slack": slack}}
    if other_providers:
        payload["channels"].update(other_providers)
    if visible_replies is not None:
        payload["messages"] = {"groupChat": {"visibleReplies": visible_replies}}
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# Schema + validation
# ─────────────────────────────────────────────────────────────────────────────


class TestValidatePolicy:
    def test_name_keyed_channel_id_rejected(self) -> None:
        policy = _minimal_policy(channels=[_entry("project-x")])
        errors = validate_policy(policy)
        assert any("not a Slack ID" in e for e in errors)

    def test_duplicate_channel_id_rejected(self) -> None:
        policy = _minimal_policy(channels=[
            _entry("G0T79FGSE"), _entry("G0T79FGSE"),
        ])
        errors = validate_policy(policy)
        assert any("duplicate" in e.lower() for e in errors)

    def test_user_group_mode_requires_group_id(self) -> None:
        policy = _minimal_policy(dm_mode="user_group")
        errors = validate_policy(policy)
        assert any("user_group_id" in e for e in errors)

    def test_clean_policy_validates(self) -> None:
        policy = _minimal_policy(channels=[_entry("G0T79FGSE")])
        assert validate_policy(policy) == []

    def test_schema_version_mismatch_flagged(self) -> None:
        policy = _minimal_policy()
        policy.schema_version = 999
        errors = validate_policy(policy)
        assert any("schema_version" in e for e in errors)


# ─────────────────────────────────────────────────────────────────────────────
# Storage round-trip
# ─────────────────────────────────────────────────────────────────────────────


class TestStorageRoundTrip:
    def test_save_then_load(self, tmp_path: Path) -> None:
        policy = _minimal_policy(
            channels=[_entry("G0T79FGSE", channel_name="ops", require_mention=False)],
            dm_mode="explicit",
            user_ids=["U0AABBCC", "U0DEADBEE"],
        )
        save_policy(tmp_path, policy)
        loaded = load_policy(tmp_path, "team_bot_a")
        assert loaded is not None
        assert loaded.bot_id == "team_bot_a"
        assert len(loaded.channels.entries) == 1
        assert loaded.channels.entries[0].channel_id == "G0T79FGSE"
        assert loaded.channels.entries[0].channel_name == "ops"
        assert loaded.channels.entries[0].require_mention is False
        assert loaded.access.dm_allowlist.mode == "explicit"
        assert sorted(loaded.access.dm_allowlist.user_ids) == ["U0AABBCC", "U0DEADBEE"]

    def test_save_refuses_invalid(self, tmp_path: Path) -> None:
        policy = _minimal_policy(channels=[_entry("project-x")])
        with pytest.raises(ValueError, match="not a Slack ID"):
            save_policy(tmp_path, policy)

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert load_policy(tmp_path, "ghost") is None

    def test_malformed_json_raises(self, tmp_path: Path) -> None:
        p = policy_path(tmp_path, "team_bot_a")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{ not json")
        with pytest.raises(ValueError, match="not valid JSON"):
            load_policy(tmp_path, "team_bot_a")

    def test_unknown_fields_ignored(self, tmp_path: Path) -> None:
        """Forward compat: a future field shouldn't break the loader."""
        p = policy_path(tmp_path, "team_bot_a")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "bot_id": "team_bot_a",
            "schema_version": SCHEMA_VERSION,
            "future_field": {"some": "thing"},
        }))
        loaded = load_policy(tmp_path, "team_bot_a")
        assert loaded is not None
        assert loaded.bot_id == "team_bot_a"


# ─────────────────────────────────────────────────────────────────────────────
# Writer — the high-risk area. Preservation contract.
# ─────────────────────────────────────────────────────────────────────────────


class TestWriterOwnershipBoundary:
    """Anything outside the spec'd ownership boundary must survive intact."""

    def test_preserves_other_provider_blocks(self) -> None:
        existing = _existing(
            bot_token="xoxb-keepme",
            slack_extras={"groupPolicy": "allowlist"},
            channels={"G0T79FGSE": {"requireMention": True}},
            other_providers={
                "telegram": {"botToken": "tg-keepme"},
                "discord": {"botToken": "dc-keepme"},
                "whatsapp": {"enabled": False, "dmPolicy": "pairing"},
            },
        )
        policy = _minimal_policy(channels=[_entry("G0T79FGSE")])
        merged, _, _, _ = merge_policy_into_openclaw(existing=existing, policy=policy)
        # Slack credentials untouched
        assert merged["channels"]["slack"]["botToken"] == "xoxb-keepme"
        # Other top-level providers untouched
        assert merged["channels"]["telegram"] == {"botToken": "tg-keepme"}
        assert merged["channels"]["discord"] == {"botToken": "dc-keepme"}
        assert merged["channels"]["whatsapp"] == {"enabled": False, "dmPolicy": "pairing"}

    def test_preserves_top_level_fields(self) -> None:
        existing = {
            "hooks": {"some": "hook"},
            "permissions": {"agent": ["read"]},
            "meta": {"lastTouchedVersion": "2026.5.13"},
            "agent": {"profile": "team_bot_a"},
            "channels": {"slack": {
                "botToken": "x",
                "channels": {"G0T79FGSE": {"requireMention": True}},
            }},
            "messages": {"directChat": {"echoMode": "off"}},
        }
        policy = _minimal_policy(channels=[_entry("G0T79FGSE")])
        merged, _, _, _ = merge_policy_into_openclaw(existing=existing, policy=policy)
        assert merged["hooks"] == {"some": "hook"}
        assert merged["permissions"] == {"agent": ["read"]}
        assert merged["meta"] == {"lastTouchedVersion": "2026.5.13"}
        assert merged["agent"] == {"profile": "team_bot_a"}
        # Other messages sub-blocks preserved
        assert merged["messages"]["directChat"] == {"echoMode": "off"}

    def test_preserves_slack_sub_block_peers(self) -> None:
        """``channels.slack.{mode,dmPolicy,streaming,...}`` must survive a render."""
        existing = _existing(
            slack_extras={
                "mode": "socket",
                "dmPolicy": "pairing",
                "enabled": True,
                "webhookPath": "/slack/events",
                "streaming": {"mode": "partial", "nativeTransport": True},
                "userTokenReadOnly": "xoxp-keepme",
            },
            channels={"G0T79FGSE": {"requireMention": True}},
        )
        policy = _minimal_policy(channels=[_entry("G0T79FGSE")])
        merged, _, _, _ = merge_policy_into_openclaw(existing=existing, policy=policy)
        slack = merged["channels"]["slack"]
        assert slack["mode"] == "socket"
        assert slack["dmPolicy"] == "pairing"
        assert slack["enabled"] is True
        assert slack["webhookPath"] == "/slack/events"
        assert slack["streaming"] == {"mode": "partial", "nativeTransport": True}
        assert slack["userTokenReadOnly"] == "xoxp-keepme"

    def test_does_not_mutate_input(self) -> None:
        """The caller's existing dict must not be modified."""
        existing = _existing(
            slack_extras={"groupPolicy": "other"},
            channels={"G0OLDABCDE": {"requireMention": False}},
        )
        snapshot = json.dumps(existing, sort_keys=True)
        policy = _minimal_policy(channels=[_entry("G0NEWABCDE")])
        merge_policy_into_openclaw(existing=existing, policy=policy)
        assert json.dumps(existing, sort_keys=True) == snapshot


class TestWriterRender:
    def test_removes_stale_slack_channels(self) -> None:
        existing = _existing(channels={
            "G0OLDABCDE": {"requireMention": False},
            "G0KEEPABC": {"requireMention": True},
        })
        policy = _minimal_policy(channels=[_entry("G0KEEPABC", require_mention=True)])
        merged, added, updated, removed = merge_policy_into_openclaw(
            existing=existing, policy=policy,
        )
        slack_channels = merged["channels"]["slack"]["channels"]
        assert "G0OLDABCDE" not in slack_channels
        assert "G0KEEPABC" in slack_channels
        assert removed == ["G0OLDABCDE"]

    def test_sets_groupPolicy_to_allowlist(self) -> None:
        existing = _existing(slack_extras={"groupPolicy": "wide_open"})
        policy = _minimal_policy(channels=[_entry("G0T79FGSE")])
        merged, _, _, _ = merge_policy_into_openclaw(existing=existing, policy=policy)
        assert merged["channels"]["slack"]["groupPolicy"] == GROUP_POLICY

    def test_sets_visibleReplies_default(self) -> None:
        existing = _existing(visible_replies=None)
        policy = _minimal_policy(
            channels=[_entry("G0T79FGSE")], visible_replies="thread_only",
        )
        merged, _, _, _ = merge_policy_into_openclaw(existing=existing, policy=policy)
        assert merged["messages"]["groupChat"]["visibleReplies"] == "thread_only"

    def test_per_channel_visibleReplies_override(self) -> None:
        existing = _existing()
        policy = _minimal_policy(channels=[
            _entry("G0T79FGSE", visible_replies="ephemeral"),
        ])
        merged, _, _, _ = merge_policy_into_openclaw(existing=existing, policy=policy)
        assert merged["channels"]["slack"]["channels"]["G0T79FGSE"]["visibleReplies"] == "ephemeral"

    def test_dm_open_removes_allowFrom(self) -> None:
        existing = _existing(allow_from=["U0OLDA"])
        policy = _minimal_policy(channels=[_entry("G0T79FGSE")], dm_mode="open")
        merged, _, _, _ = merge_policy_into_openclaw(existing=existing, policy=policy)
        assert "allowFrom" not in merged["channels"]["slack"]

    def test_dm_explicit_writes_sorted_allowFrom(self) -> None:
        existing = _existing()
        policy = _minimal_policy(
            channels=[_entry("G0T79FGSE")],
            dm_mode="explicit",
            user_ids=["U0BBB", "U0AAA", "U0CCC"],
        )
        merged, _, _, _ = merge_policy_into_openclaw(existing=existing, policy=policy)
        assert merged["channels"]["slack"]["allowFrom"] == ["U0AAA", "U0BBB", "U0CCC"]

    def test_diff_buckets_classified_correctly(self) -> None:
        existing = _existing(channels={
            "G0KEEPABCSAMEX": {"requireMention": True, "displayName": "keep"},
            "G0CHANGEAB":     {"requireMention": True},
            "G0REMOVEAB":     {"requireMention": False},
        })
        policy = _minimal_policy(channels=[
            _entry("G0KEEPABCSAMEX", channel_name="keep", require_mention=True),
            _entry("G0CHANGEAB", require_mention=False),
            _entry("G0NEWABCDE"),
        ])
        merged, added, updated, removed = merge_policy_into_openclaw(
            existing=existing, policy=policy,
        )
        assert added == ["G0NEWABCDE"]
        assert updated == ["G0CHANGEAB"]
        assert removed == ["G0REMOVEAB"]


class TestRenderUpToDate:
    def test_matching_render_is_up_to_date(self) -> None:
        policy = _minimal_policy(channels=[_entry("G0T79FGSE", channel_name="ops")])
        existing, _, _, _ = merge_policy_into_openclaw(existing=_existing(), policy=policy)
        assert is_render_up_to_date(existing=existing, policy=policy)

    def test_extra_slack_channel_is_drift(self) -> None:
        policy = _minimal_policy(channels=[_entry("G0LISTEDXX")])
        existing = _existing(
            slack_extras={"groupPolicy": GROUP_POLICY},
            channels={
                "G0LISTEDXX": {"requireMention": True},
                "G0EXTRAXXX": {"requireMention": True},  # not in policy
            },
        )
        assert not is_render_up_to_date(existing=existing, policy=policy)


# ─────────────────────────────────────────────────────────────────────────────
# Synthesize → save → re-render round trip
# ─────────────────────────────────────────────────────────────────────────────


class TestSynthesizeBootstrap:
    def _bot_home_with_openclaw(self, tmp_path: Path, payload: dict) -> Path:
        home = tmp_path / "team_bot_a_home"
        (home / ".openclaw").mkdir(parents=True)
        (home / ".openclaw" / "openclaw.json").write_text(json.dumps(payload))
        return home

    def test_clean_openclaw_round_trips(self, tmp_path: Path) -> None:
        home = self._bot_home_with_openclaw(tmp_path, _existing(
            slack_extras={"groupPolicy": "allowlist"},
            channels={"G0T79FGSE": {"requireMention": False, "displayName": "ops"}},
        ))
        policy, err = synthesize_policy_from_openclaw(bot_id="team_bot_a", bot_home=home)
        assert err is None
        assert policy is not None
        assert len(policy.channels.entries) == 1
        entry = policy.channels.entries[0]
        assert entry.channel_id == "G0T79FGSE"
        assert entry.require_mention is False
        assert entry.channel_name == "ops"
        assert entry.channel_type == "private"  # G… prefix

    def test_name_keyed_entries_skipped(self, tmp_path: Path) -> None:
        """Synthesize MUST NOT encode name-keyed entries (would re-render bug 1)."""
        home = self._bot_home_with_openclaw(tmp_path, _existing(
            slack_extras={"groupPolicy": "allowlist"},
            channels={
                "project-x": {"requireMention": False},  # SLK001 — must be dropped
                "G0KEEPABC": {"requireMention": True},
            },
        ))
        policy, err = synthesize_policy_from_openclaw(bot_id="team_bot_a", bot_home=home)
        assert err is None
        assert policy is not None
        ids = [e.channel_id for e in policy.channels.entries]
        assert "G0KEEPABC" in ids
        assert "project-x" not in ids

    def test_allow_from_round_trips_as_explicit(self, tmp_path: Path) -> None:
        """``channels.slack.allowFrom`` should synthesize to dm_mode=explicit."""
        home = self._bot_home_with_openclaw(tmp_path, _existing(
            channels={"G0T79FGSE": {"requireMention": True}},
            allow_from=["U0A", "U0B"],
        ))
        policy, err = synthesize_policy_from_openclaw(bot_id="team_bot_a", bot_home=home)
        assert err is None
        assert policy is not None
        assert policy.access.dm_allowlist.mode == "explicit"
        assert sorted(policy.access.dm_allowlist.user_ids) == ["U0A", "U0B"]

    def test_synthesize_then_render_is_idempotent(self, tmp_path: Path) -> None:
        """Bootstrap → save → re-render must produce the same openclaw.json.

        This is the spec's "round-trip" promise. If a future change to the
        writer drops a field synthesize captured (or vice versa), this
        catches it.
        """
        original = _existing(
            slack_extras={"groupPolicy": "allowlist", "mode": "socket"},
            channels={
                "G0T79FGSE": {"requireMention": False, "displayName": "ops"},
                "C0AL2GDUA7J": {"requireMention": True, "displayName": "project-x"},
            },
            allow_from=["U0A", "U0B"],
        )
        home = self._bot_home_with_openclaw(tmp_path, original)
        policy, err = synthesize_policy_from_openclaw(bot_id="team_bot_a", bot_home=home)
        assert err is None and policy is not None
        rendered, _, _, _ = merge_policy_into_openclaw(
            existing=original, policy=policy,
        )
        # The rendered openclaw.json should equal the original (modulo
        # field reordering by Python dict).
        assert rendered == original

    def test_missing_openclaw_returns_error(self, tmp_path: Path) -> None:
        home = tmp_path / "no_bot"
        home.mkdir()
        policy, err = synthesize_policy_from_openclaw(bot_id="ghost", bot_home=home)
        assert policy is None
        assert err == "openclaw_json_missing"


# ─────────────────────────────────────────────────────────────────────────────
# Drift detection (SLK011)
# ─────────────────────────────────────────────────────────────────────────────


class TestReviewerFixes:
    """Regression coverage for the 8 fixes from Phase 2's independent review.

    Each test pins one silent-failure mode the reviewer caught. If a
    future refactor reintroduces the bug, the matching test fails.
    """

    # #1 — Writer must sweep stale name-keyed entries from channels.slack.channels
    def test_writer_sweeps_name_keyed_slack_shaped_entries(self) -> None:
        existing = _existing(channels={
            "project-x": {"requireMention": False},   # bug 1 leftover
            "#management": {"requireMention": True},    # bug 1 with #
            "G0KEEPABC": {"requireMention": True},
        })
        policy = _minimal_policy(channels=[_entry("G0KEEPABC")])
        merged, _, _, removed = merge_policy_into_openclaw(
            existing=existing, policy=policy,
        )
        slack_channels = merged["channels"]["slack"]["channels"]
        assert "project-x" not in slack_channels
        assert "#management" not in slack_channels
        assert "project-x" in removed
        assert "#management" in removed

    def test_writer_leaves_top_level_provider_blocks_alone(self) -> None:
        """Top-level ``channels.<other_provider>`` blocks (telegram, whatsapp)
        live in a separate namespace from the Slack channel allowlist and
        must survive a Slack render untouched.
        """
        existing = _existing(
            channels={"G0KEEPABC": {"requireMention": True}},
            other_providers={
                "telegram": {"botToken": "tg-x", "enabled": True},
                "whatsapp": {"enabled": False, "dmPolicy": "pairing"},
            },
        )
        policy = _minimal_policy(channels=[_entry("G0KEEPABC")])
        merged, _, _, _ = merge_policy_into_openclaw(
            existing=existing, policy=policy,
        )
        assert merged["channels"]["telegram"] == {"botToken": "tg-x", "enabled": True}
        assert merged["channels"]["whatsapp"] == {"enabled": False, "dmPolicy": "pairing"}

    # #3 — Stale user allowlist cleared on mode change
    def test_dm_mode_switch_from_explicit_to_user_group_clears_allowFrom(self) -> None:
        existing = _existing(allow_from=["U0OLD1", "U0OLD2"])
        policy = _minimal_policy(
            channels=[_entry("G0KEEPABC")], dm_mode="user_group",
        )
        policy.access.dm_allowlist.user_group_id = "S0GROUP"
        merged, _, _, _ = merge_policy_into_openclaw(
            existing=existing, policy=policy,
        )
        # user_group mode: writer can't resolve offline, but stale list
        # must NOT persist — safe state is "no allowlist".
        assert "allowFrom" not in merged["channels"]["slack"]

    # #4 — Writer preserves unknown per-channel fields under channels.slack.channels.<ID>
    def test_writer_preserves_unknown_per_channel_fields(self) -> None:
        existing = _existing(channels={
            "G0KEEPABC": {
                "requireMention": True,
                "customRouting": "foo",     # unknown to writer
                "futureField": 42,           # also unknown
            },
        })
        policy = _minimal_policy(channels=[_entry("G0KEEPABC", require_mention=False)])
        merged, _, _, _ = merge_policy_into_openclaw(
            existing=existing, policy=policy,
        )
        entry = merged["channels"]["slack"]["channels"]["G0KEEPABC"]
        assert entry["requireMention"] is False  # writer's update
        assert entry["customRouting"] == "foo"   # preserved
        assert entry["futureField"] == 42        # preserved

    # #6 — Strict enum validation on load
    def test_load_raises_on_unknown_dm_mode(self, tmp_path: Path) -> None:
        p = policy_path(tmp_path, "team_bot_a")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "bot_id": "team_bot_a",
            "schema_version": SCHEMA_VERSION,
            "access": {"dm_allowlist": {"mode": "bogus_mode"}},
        }))
        with pytest.raises(ValueError, match="bogus_mode"):
            load_policy(tmp_path, "team_bot_a")

    # #7 — show --json redacts unconsumed self-enrollment codes
    def test_to_dict_redacted_hides_unconsumed_codes(self) -> None:
        from evolve_admin.integrations.slack.policy import (
            SelfEnrollmentCode, to_dict_redacted,
        )
        policy = _minimal_policy(channels=[_entry("G0KEEPABC")])
        policy.access.self_enrollment_codes = [
            SelfEnrollmentCode(
                code="racecar", created_by="U0A", created_at="t", expires_at="t",
            ),
            SelfEnrollmentCode(
                code="alreadyUsed", created_by="U0A", created_at="t", expires_at="t",
                consumed=True, consumed_by="U0DAVE", consumed_at="t",
            ),
        ]
        payload = to_dict_redacted(policy)
        codes = payload["access"]["self_enrollment_codes"]
        # Unconsumed → redacted; consumed → preserved (the code is no longer secret)
        unconsumed = next(c for c in codes if not c["consumed"])
        consumed = next(c for c in codes if c["consumed"])
        assert unconsumed["code"] == "<redacted>"
        assert consumed["code"] == "alreadyUsed"

    # #8 — Per-channel visible_replies enum validation
    def test_validate_rejects_bad_per_channel_visible_replies(self) -> None:
        policy = _minimal_policy(channels=[
            _entry("G0KEEPABC", visible_replies="thread_olny"),  # typo
        ])
        errors = validate_policy(policy)
        assert any("thread_olny" in e for e in errors)

    def test_validate_rejects_bad_default_visible_replies(self) -> None:
        policy = _minimal_policy(channels=[_entry("G0KEEPABC")])
        policy.messaging.visible_replies_default = "thread_olny"
        errors = validate_policy(policy)
        assert any("visible_replies_default" in e for e in errors)


class TestSlk011PolicyDrift:
    """Doctor-level integration. Verifies SLK011 fires only when expected."""

    def _setup(self, tmp_path: Path, *, openclaw: dict, policy: SlackPolicy | None):
        home = tmp_path / "bot_home"
        (home / ".openclaw").mkdir(parents=True)
        (home / ".openclaw" / "openclaw.json").write_text(json.dumps(openclaw))
        shared_dir = tmp_path / "shared"
        if policy is not None:
            save_policy(shared_dir, policy)
        return home, shared_dir

    def test_no_policy_means_no_drift_check(self, tmp_path: Path) -> None:
        from evolve_admin.integrations.slack.doctor import run_doctor
        home, shared = self._setup(tmp_path, openclaw=_existing(
            slack_extras={"groupPolicy": "allowlist"},
            channels={"G0T79FGSE": {"requireMention": False}},
        ), policy=None)
        from .test_slack_doctor import FakeSlackClient
        client = FakeSlackClient(member_channels=[{"id": "G0T79FGSE", "name": "ok"}])
        result = run_doctor(
            "team_bot_a", bot_home=home, shared_dir=shared,
            slack_client_factory=lambda _t: client,
        )
        assert not any(f.code == "SLK011" for f in result.findings)

    def test_matching_policy_no_drift(self, tmp_path: Path) -> None:
        from evolve_admin.integrations.slack.doctor import run_doctor
        from evolve_admin.integrations.slack.writer import merge_policy_into_openclaw
        # Build a policy then synthesize an openclaw.json that matches it.
        policy = _minimal_policy(
            bot_id="team_bot_a",
            channels=[_entry("G0T79FGSE", channel_name="ops", require_mention=False)],
        )
        baseline = _existing()
        rendered, _, _, _ = merge_policy_into_openclaw(
            existing=baseline, policy=policy,
        )
        home, shared = self._setup(tmp_path, openclaw=rendered, policy=policy)
        from .test_slack_doctor import FakeSlackClient
        client = FakeSlackClient(member_channels=[{"id": "G0T79FGSE", "name": "ops"}])
        result = run_doctor(
            "team_bot_a", bot_home=home, shared_dir=shared,
            slack_client_factory=lambda _t: client,
        )
        assert not any(f.code == "SLK011" for f in result.findings)

    def test_extra_channel_in_openclaw_triggers_slk011(self, tmp_path: Path) -> None:
        from evolve_admin.integrations.slack.doctor import run_doctor
        policy = _minimal_policy(
            bot_id="team_bot_a", channels=[_entry("G0LISTEDXX")],
        )
        # openclaw has an extra channel the policy doesn't know about
        openclaw = _existing(
            slack_extras={"groupPolicy": GROUP_POLICY},
            channels={
                "G0LISTEDXX": {"requireMention": True},
                "G0EXTRAXXX": {"requireMention": True},
            },
        )
        home, shared = self._setup(tmp_path, openclaw=openclaw, policy=policy)
        from .test_slack_doctor import FakeSlackClient
        client = FakeSlackClient(member_channels=[
            {"id": "G0LISTEDXX", "name": "a"}, {"id": "G0EXTRAXXX", "name": "b"},
        ])
        result = run_doctor(
            "team_bot_a", bot_home=home, shared_dir=shared,
            slack_client_factory=lambda _t: client,
        )
        slk011 = [f for f in result.findings if f.code == "SLK011"]
        assert len(slk011) == 1
        assert slk011[0].severity == "warn"

    def test_malformed_policy_triggers_fail(self, tmp_path: Path) -> None:
        from evolve_admin.integrations.slack.doctor import run_doctor
        home = tmp_path / "bot_home"
        (home / ".openclaw").mkdir(parents=True)
        (home / ".openclaw" / "openclaw.json").write_text(json.dumps(_existing()))
        shared = tmp_path / "shared"
        p = policy_path(shared, "team_bot_a")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{ not valid json")
        result = run_doctor("team_bot_a", bot_home=home, shared_dir=shared)
        slk011 = [f for f in result.findings if f.code == "SLK011"]
        assert len(slk011) == 1
        assert slk011[0].severity == "fail"
