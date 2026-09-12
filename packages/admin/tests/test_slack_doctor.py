"""Tests for evolve_admin.integrations.slack.doctor.

The doctor's job is to catch silent failure modes that have shipped in
real openclaw.json files (most recently team_bot_a's bug 1 + bug 2). Each
test below pins one of those modes — if a future refactor breaks the
check, the matching test fails.

Slack API calls are mocked via a ``FakeSlackClient`` injected through
``run_doctor``'s ``slack_client_factory`` hook so no test ever touches
the network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from evolve_admin.integrations.slack.doctor import run_doctor
from evolve_admin.integrations.slack.oc_config import is_slack_channel_id
from evolve_admin.integrations.slack.slack_client import SlackError


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class FakeSlackClient:
    """Stand-in for SlackClient. Only the methods the doctor calls."""
    auth_response: dict | None = field(default_factory=lambda: {
        "user_id": "U0BOT", "user": "team_bot_a", "team_id": "T123", "team": "Test", "bot_id": "B1",
    })
    auth_error: SlackError | None = None
    member_channels: list[dict] = field(default_factory=list)
    members_error: SlackError | None = None
    # Group DMs (mpim) the bot participates in — returned only for the
    # doctor's separate ``users.conversations(types="mpim")`` lookup, never
    # merged into the public/private membership the default call returns.
    mpim_channels: list[dict] = field(default_factory=list)
    mpim_error: SlackError | None = None
    known_user_ids: set = field(default_factory=lambda: {"U0BOT"})

    def auth_test(self):
        if self.auth_error is not None:
            return None, self.auth_error
        return self.auth_response, None

    def users_conversations(self, *, types="public_channel,private_channel", **_):
        if "mpim" in types:
            if self.mpim_error is not None:
                return None, self.mpim_error
            return list(self.mpim_channels), None
        if self.members_error is not None:
            return None, self.members_error
        return list(self.member_channels), None

    def users_info(self, user_id: str):
        if user_id in self.known_user_ids:
            return {"id": user_id, "name": "fake"}, None
        return None, SlackError(
            code="api_error", detail="not found", slack_error="user_not_found",
        )


def _make_bot_home(tmp_path: Path, *, openclaw_payload: dict | None) -> Path:
    """Build a minimal bot-home tree with an openclaw.json under .openclaw/."""
    home = tmp_path / "fake_bot"
    (home / ".openclaw").mkdir(parents=True, exist_ok=True)
    if openclaw_payload is not None:
        (home / ".openclaw" / "openclaw.json").write_text(
            json.dumps(openclaw_payload, indent=2)
        )
    return home


def _payload(
    *,
    bot_token: str | None = "xoxb-test",
    group_policy: str | None = "allowlist",
    channels: dict | None = None,
    visible_replies: str | None = "automatic",
    dm_allowed: list[str] | None = None,
    extra_top_level_channels: dict | None = None,
) -> dict:
    """Build an openclaw.json payload matching the real OC Slack shape.

    Channel entries go under ``channels.slack.channels.<KEY>``; the user
    allowlist (formerly thought to live at ``directMessages.allowedUserIds``)
    actually lives at ``channels.slack.allowFrom``. The ``channels``
    argument keeps its old name for caller readability but populates the
    nested location.

    ``extra_top_level_channels`` injects sibling provider sub-blocks
    (telegram, whatsapp, etc.) at the top level for tests that care
    about provider-block preservation.
    """
    slack_block: dict = {}
    if bot_token is not None:
        slack_block["botToken"] = bot_token
    if group_policy is not None:
        slack_block["groupPolicy"] = group_policy
    if channels:
        slack_block["channels"] = dict(channels)
    if dm_allowed is not None:
        slack_block["allowFrom"] = list(dm_allowed)

    channels_block: dict = {}
    if slack_block:
        channels_block["slack"] = slack_block
    if extra_top_level_channels:
        channels_block.update(extra_top_level_channels)

    messages_block: dict = {}
    if visible_replies is not None:
        messages_block["groupChat"] = {"visibleReplies": visible_replies}

    payload: dict = {}
    if channels_block:
        payload["channels"] = channels_block
    if messages_block:
        payload["messages"] = messages_block
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers
# ─────────────────────────────────────────────────────────────────────────────


class TestIsSlackChannelId:
    def test_recognizes_public_channel(self) -> None:
        assert is_slack_channel_id("C0AL2GDUA7J")

    def test_recognizes_private_channel(self) -> None:
        assert is_slack_channel_id("G0T79FGSE")

    def test_rejects_name(self) -> None:
        assert not is_slack_channel_id("management")
        assert not is_slack_channel_id("project-x")
        assert not is_slack_channel_id("#general")

    def test_rejects_lowercase(self) -> None:
        assert not is_slack_channel_id("c0al2gdua7j")

    def test_rejects_too_short(self) -> None:
        assert not is_slack_channel_id("C0AL")


# ─────────────────────────────────────────────────────────────────────────────
# SLK001 — name-keyed channels under allowlist (bug 1)
# ─────────────────────────────────────────────────────────────────────────────


class TestSlk001NameKeyedChannels:
    def test_name_keyed_under_allowlist_is_fail(self, tmp_path: Path) -> None:
        """team_bot_a's exact bug: 'project-x' key under allowlist policy."""
        payload = _payload(
            channels={
                "project-x": {"requireMention": False},
                "G0T79FGSE": {"requireMention": False},
            },
        )
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        client = FakeSlackClient(member_channels=[{"id": "G0T79FGSE", "name": "ok"}])
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)

        slk001 = [f for f in result.findings if f.code == "SLK001"]
        assert len(slk001) == 1
        assert slk001[0].severity == "fail"
        assert "project-x" in slk001[0].title
        assert slk001[0].fixable

    def test_name_keyed_without_allowlist_is_still_fail(self, tmp_path: Path) -> None:
        """Name-keying is always FAIL — OC routes by ID regardless of policy."""
        payload = _payload(
            group_policy="default",
            channels={"project-x": {"requireMention": False}},
        )
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        client = FakeSlackClient()
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)

        slk001 = [f for f in result.findings if f.code == "SLK001"]
        assert len(slk001) == 1
        assert slk001[0].severity == "fail"

    def test_id_keyed_no_finding(self, tmp_path: Path) -> None:
        payload = _payload(
            channels={"G0T79FGSE": {"requireMention": False}},
        )
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        client = FakeSlackClient(member_channels=[{"id": "G0T79FGSE", "name": "x"}])
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        assert not any(f.code == "SLK001" for f in result.findings)


# ─────────────────────────────────────────────────────────────────────────────
# SLK002 — keyed ID not in member channels
# ─────────────────────────────────────────────────────────────────────────────


class TestSlk002BotNotInChannel:
    def test_id_in_config_but_bot_not_a_member(self, tmp_path: Path) -> None:
        payload = _payload(
            channels={"G0T79FGSE": {"requireMention": False}},
        )
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        # Bot is NOT a member of G0T79FGSE — it's only in C9999.
        client = FakeSlackClient(member_channels=[{"id": "C9999", "name": "other"}])
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)

        slk002 = [f for f in result.findings if f.code == "SLK002"]
        assert len(slk002) == 1
        assert slk002[0].severity == "fail"
        assert slk002[0].channel_id == "G0T79FGSE"


# ─────────────────────────────────────────────────────────────────────────────
# SLK019 — group-DM (mpim) ID in the channel allowlist
# ─────────────────────────────────────────────────────────────────────────────


class TestSlk019GroupDmInAllowlist:
    """Group DM IDs are C-prefixed in newer workspaces and indistinguishable
    from a real channel by shape, but OC routes them via dmPolicy — never the
    channel allowlist. So a group-DM entry must NOT fire the deploy-blocking
    SLK002 FAIL when the bot is genuinely a participant.
    """

    def test_group_dm_bot_participates_is_not_slk002_fail(self, tmp_path: Path) -> None:
        """The latent false-FAIL: a group-DM ID the bot participates in is
        never in the public/private membership list, so it used to fire
        SLK002 FAIL (blocking a Phase-2 deploy). It must instead be a
        non-blocking SLK019 WARN."""
        payload = _payload(channels={"C0B7PDLM2PJ": {"requireMention": False}})
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        # C0B7PDLM2PJ is C-prefixed (looks like a channel) but is a group DM
        # the bot participates in — it shows up only in the mpim listing.
        client = FakeSlackClient(
            member_channels=[],
            mpim_channels=[{"id": "C0B7PDLM2PJ", "name": "mpdm-a--b--c-1"}],
        )
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)

        assert not any(f.code == "SLK002" for f in result.findings)
        slk019 = [f for f in result.findings if f.code == "SLK019"]
        assert len(slk019) == 1
        assert slk019[0].severity == "warn"
        assert slk019[0].channel_id == "C0B7PDLM2PJ"

    def test_group_dm_bot_not_participant_still_slk002_fail(self, tmp_path: Path) -> None:
        """The originating real-world case: a group-DM ID is in the allowlist
        but the bot is NOT a participant (not in its mpim list). The finding is
        genuinely correct — SLK002 FAIL stands, no SLK019."""
        payload = _payload(channels={"C0B7PDLM2PJ": {"requireMention": False}})
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        client = FakeSlackClient(member_channels=[], mpim_channels=[])
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)

        slk002 = [f for f in result.findings if f.code == "SLK002"]
        assert len(slk002) == 1
        assert slk002[0].severity == "fail"
        assert not any(f.code == "SLK019" for f in result.findings)

    def test_group_dm_membership_does_not_pollute_slk005(self, tmp_path: Path) -> None:
        """A group DM the bot participates in but that is NOT in the channel
        allowlist must not surface as an SLK005 uncovered channel — group DMs
        are DM-policy territory, queried separately and never merged into the
        member map SLK005 scans."""
        payload = _payload(channels={"G0T79FGSE": {"requireMention": False}})
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        client = FakeSlackClient(
            member_channels=[{"id": "G0T79FGSE", "name": "ops"}],
            mpim_channels=[{"id": "C0B7PDLM2PJ", "name": "mpdm-x--y--z-1"}],
        )
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)

        slk005 = [f for f in result.findings if f.code == "SLK005"]
        assert all(f.channel_id != "C0B7PDLM2PJ" for f in slk005)
        assert not slk005  # G0T79FGSE is covered; the group DM is not uncovered

    def test_inconclusive_mpim_lookup_falls_back_to_fail(self, tmp_path: Path) -> None:
        """If mpim membership can't be resolved (e.g. missing mpim:read
        scope), don't suppress — fall back to the historical SLK002 FAIL so a
        genuinely-missing channel is still caught."""
        payload = _payload(channels={"C0B7PDLM2PJ": {"requireMention": False}})
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        client = FakeSlackClient(
            member_channels=[],
            mpim_error=SlackError(
                code="api_error", detail="missing scope", slack_error="missing_scope",
            ),
        )
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)

        slk002 = [f for f in result.findings if f.code == "SLK002"]
        assert len(slk002) == 1
        assert slk002[0].severity == "fail"
        assert not any(f.code == "SLK019" for f in result.findings)


# ─────────────────────────────────────────────────────────────────────────────
# SLK003 — visibleReplies default (bug 2)
# ─────────────────────────────────────────────────────────────────────────────


class TestSlk003VisibleReplies:
    def test_missing_is_warn(self, tmp_path: Path) -> None:
        """team_bot_a's exact bug 2: no visibleReplies → OC default silently changes."""
        payload = _payload(
            channels={"G0T79FGSE": {"requireMention": False}},
            visible_replies=None,
        )
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        client = FakeSlackClient(member_channels=[{"id": "G0T79FGSE", "name": "ok"}])
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)

        slk003 = [f for f in result.findings if f.code == "SLK003"]
        assert len(slk003) == 1
        assert slk003[0].severity == "warn"

    def test_automatic_is_no_finding(self, tmp_path: Path) -> None:
        payload = _payload(
            channels={"G0T79FGSE": {"requireMention": False}},
            visible_replies="automatic",
        )
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        client = FakeSlackClient(member_channels=[{"id": "G0T79FGSE", "name": "ok"}])
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        assert not any(f.code == "SLK003" for f in result.findings)

    def test_non_default_is_info(self, tmp_path: Path) -> None:
        payload = _payload(
            channels={"G0T79FGSE": {"requireMention": False}},
            visible_replies="thread_only",
        )
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        client = FakeSlackClient(member_channels=[{"id": "G0T79FGSE", "name": "ok"}])
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        slk003 = [f for f in result.findings if f.code == "SLK003"]
        assert len(slk003) == 1
        assert slk003[0].severity == "info"


# ─────────────────────────────────────────────────────────────────────────────
# SLK004 — DM allowlist unknown user
# ─────────────────────────────────────────────────────────────────────────────


class TestSlk004DmAllowlist:
    def test_unknown_user_id_is_fail(self, tmp_path: Path) -> None:
        payload = _payload(
            channels={"G0T79FGSE": {"requireMention": False}},
            dm_allowed=["U0AABBCC", "U0DEADBEEF"],
        )
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        client = FakeSlackClient(
            member_channels=[{"id": "G0T79FGSE", "name": "ok"}],
            known_user_ids={"U0AABBCC"},
        )
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        slk004 = [f for f in result.findings if f.code == "SLK004"]
        assert len(slk004) == 1
        assert slk004[0].user_id == "U0DEADBEEF"
        assert slk004[0].severity == "fail"

    def test_non_id_shaped_entry_is_fail(self, tmp_path: Path) -> None:
        payload = _payload(
            channels={"G0T79FGSE": {"requireMention": False}},
            dm_allowed=["dave@example.com"],
        )
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        client = FakeSlackClient(member_channels=[{"id": "G0T79FGSE", "name": "ok"}])
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        slk004 = [f for f in result.findings if f.code == "SLK004"]
        assert len(slk004) == 1
        assert slk004[0].severity == "fail"

    def test_transport_error_aborts_loop_with_one_warn(self, tmp_path: Path) -> None:
        """First transport error → one aggregate WARN, not N per-user WARNs.

        Prevents the signal storm the reviewer flagged: if Slack is
        rate-limiting during the probe, a 50-user allowlist must not
        produce 50 Signals every 15 minutes.
        """
        payload = _payload(
            channels={"G0T79FGSE": {"requireMention": False}},
            dm_allowed=["U0FIRST", "U0SECOND", "U0THIRD", "U0FOURTH"],
        )
        home = _make_bot_home(tmp_path, openclaw_payload=payload)

        @dataclass
        class FlakyUsersInfoClient(FakeSlackClient):
            calls_made: int = 0

            def users_info(self, user_id: str):
                self.calls_made += 1
                from evolve_admin.integrations.slack.slack_client import SlackError
                return None, SlackError(
                    code="transport_error", detail="connection reset",
                )

        client = FlakyUsersInfoClient(member_channels=[{"id": "G0T79FGSE", "name": "ok"}])
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        slk004 = [f for f in result.findings if f.code == "SLK004"]
        # One aggregate WARN, not four per-user WARNs.
        assert len(slk004) == 1
        assert slk004[0].severity == "warn"
        # Loop aborted on first failure — only one API call should have happened.
        assert client.calls_made == 1


# ─────────────────────────────────────────────────────────────────────────────
# SLK005 — bot in channel not in allowlist
# ─────────────────────────────────────────────────────────────────────────────


class TestSlk005UncoveredChannels:
    def test_member_of_unlisted_channel_is_info(self, tmp_path: Path) -> None:
        payload = _payload(
            channels={"G0T79FGSE": {"requireMention": False}},
        )
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        client = FakeSlackClient(member_channels=[
            {"id": "G0T79FGSE", "name": "listed"},
            {"id": "C9999XX", "name": "design"},
        ])
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        slk005 = [f for f in result.findings if f.code == "SLK005"]
        assert len(slk005) == 1
        assert slk005[0].channel_id == "C9999XX"
        assert slk005[0].severity == "info"

    def test_name_keyed_entry_covers_member_channel_no_double_fire(
        self, tmp_path: Path,
    ) -> None:
        """SLK001 + SLK005 must not both fire for the same channel.

        Operator wrote channels."project-x" (name-keyed — SLK001 FAIL).
        Bot is a member of channel C0AL2GDUA7J named "project-x".
        We expect ONE SLK001 (name-keyed) and ZERO SLK005 — the
        operator's intent to cover the channel is clear; firing SLK005
        too leads them to add a redundant entry instead of rekeying.
        """
        payload = _payload(channels={"project-x": {"requireMention": False}})
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        client = FakeSlackClient(member_channels=[
            {"id": "C0AL2GDUA7J", "name": "project-x"},
        ])
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        assert len([f for f in result.findings if f.code == "SLK001"]) == 1
        assert len([f for f in result.findings if f.code == "SLK005"]) == 0


# ─────────────────────────────────────────────────────────────────────────────
# SLK009 — channel rename
# ─────────────────────────────────────────────────────────────────────────────


class TestSlk009ChannelRename:
    def test_cached_displayname_mismatch_is_info(self, tmp_path: Path) -> None:
        payload = _payload(
            channels={
                "G0T79FGSE": {"requireMention": False, "displayName": "old-name"},
            },
        )
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        client = FakeSlackClient(member_channels=[
            {"id": "G0T79FGSE", "name": "new-name"},
        ])
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        slk009 = [f for f in result.findings if f.code == "SLK009"]
        assert len(slk009) == 1
        assert slk009[0].severity == "info"
        assert "old-name" in slk009[0].title
        assert "new-name" in slk009[0].title

    def test_matching_displayname_no_finding(self, tmp_path: Path) -> None:
        payload = _payload(
            channels={
                "G0T79FGSE": {"requireMention": False, "displayName": "match"},
            },
        )
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        client = FakeSlackClient(member_channels=[
            {"id": "G0T79FGSE", "name": "match"},
        ])
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        assert not any(f.code == "SLK009" for f in result.findings)


# ─────────────────────────────────────────────────────────────────────────────
# SLK007 — auth / token
# ─────────────────────────────────────────────────────────────────────────────


class TestSlk007Auth:
    def test_slack_block_present_but_no_token_is_info(self, tmp_path: Path) -> None:
        """`channels.slack` exists (operator started setup) but no token yet."""
        payload = _payload(bot_token=None)
        # Slack block is present (groupPolicy: "allowlist"), just no token.
        assert "slack" in payload["channels"]
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        result = run_doctor("team_bot_a", bot_home=home)
        slk007 = [f for f in result.findings if f.code == "SLK007"]
        assert len(slk007) == 1
        assert slk007[0].severity == "info"

    def test_no_slack_block_at_all_is_no_finding(self, tmp_path: Path) -> None:
        """Personal_bot-shaped: only Telegram configured, no `channels.slack` block.

        The doctor must be silent — `messages.groupChat.visibleReplies`
        is Slack-specific, and firing SLK003 on a Telegram-only bot is
        noise (personal_bot-2026-05-19). No SLK003, no SLK007 INFO; nothing at
        all from the Slack doctor since the bot doesn't use Slack.
        """
        payload = {
            "channels": {
                "telegram": {"botToken": "tg-fake"},
            },
            # No messages.groupChat block — typical for a non-Slack bot.
        }
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        result = run_doctor("personal_bot", bot_home=home)
        assert result.findings == [], (
            f"expected no findings for a non-Slack bot, got "
            f"{[(f.code, f.severity) for f in result.findings]}"
        )

    def test_auth_failure_is_fail(self, tmp_path: Path) -> None:
        payload = _payload()
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        client = FakeSlackClient(
            auth_response=None,
            auth_error=SlackError(code="api_error", detail="", slack_error="invalid_auth"),
        )
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        slk007_fails = [
            f for f in result.findings if f.code == "SLK007" and f.severity == "fail"
        ]
        assert len(slk007_fails) == 1


# ─────────────────────────────────────────────────────────────────────────────
# SLK010 — empty allowlist black-hole
# ─────────────────────────────────────────────────────────────────────────────


class TestSlk010EmptyAllowlist:
    def test_empty_allowlist_with_member_channels_is_fail(self, tmp_path: Path) -> None:
        payload = _payload(channels=None)  # no per-channel entries
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        client = FakeSlackClient(member_channels=[{"id": "C123", "name": "general"}])
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        slk010 = [f for f in result.findings if f.code == "SLK010"]
        assert len(slk010) == 1
        assert slk010[0].severity == "fail"

    def test_empty_allowlist_with_no_member_channels_is_info(self, tmp_path: Path) -> None:
        payload = _payload(channels=None)
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        client = FakeSlackClient(member_channels=[])
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        slk010 = [f for f in result.findings if f.code == "SLK010"]
        assert len(slk010) == 1
        assert slk010[0].severity == "info"

    def test_empty_allowlist_unverified_is_warn(self, tmp_path: Path) -> None:
        """When auth fails, SLK010 downgrades to WARN (can't confirm member channels)."""
        payload = _payload(channels=None)
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        client = FakeSlackClient(
            auth_error=SlackError(code="api_error", detail="", slack_error="invalid_auth"),
        )
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        slk010 = [f for f in result.findings if f.code == "SLK010"]
        assert len(slk010) == 1
        assert slk010[0].severity == "warn"


# ─────────────────────────────────────────────────────────────────────────────
# Half-installed tolerance
# ─────────────────────────────────────────────────────────────────────────────


class TestHalfInstalled:
    def test_no_openclaw_json_is_noop(self, tmp_path: Path) -> None:
        """Brand-new bot directory — doctor returns no findings."""
        home = tmp_path / "fresh_bot"
        home.mkdir()
        result = run_doctor("fresh", bot_home=home)
        assert result.findings == []
        assert not result.has_fail()

    def test_unparseable_openclaw_is_fail(self, tmp_path: Path) -> None:
        home = tmp_path / "broken_bot"
        (home / ".openclaw").mkdir(parents=True)
        (home / ".openclaw" / "openclaw.json").write_text("{ not valid json")
        result = run_doctor("broken", bot_home=home)
        assert result.has_fail()
        assert any(f.code == "SLK000" for f in result.findings)


# ─────────────────────────────────────────────────────────────────────────────
# Green path — every well-configured field, no findings
# ─────────────────────────────────────────────────────────────────────────────


class TestDotenvTokenReader:
    """Token discovery from ~/.openclaw/workspace/.env (team_bot_a-style layout)."""

    def test_plain_assignment(self, tmp_path: Path) -> None:
        from evolve_admin.integrations.slack.oc_config import load_openclaw_view
        home = tmp_path / "bot"
        (home / ".openclaw").mkdir(parents=True)
        (home / ".openclaw" / "openclaw.json").write_text(
            json.dumps({"channels": {"slack": {}}})  # slack block present, no token
        )
        (home / ".openclaw" / "workspace").mkdir(parents=True)
        (home / ".openclaw" / "workspace" / ".env").write_text(
            "SLACK_BOT_TOKEN=xoxb-from-env\n"
        )
        view = load_openclaw_view("bot", home)
        assert view.bot_token == "xoxb-from-env"
        assert view.bot_token_source == "workspace_dotenv"

    def test_export_prefix(self, tmp_path: Path) -> None:
        from evolve_admin.integrations.slack.oc_config import load_openclaw_view
        home = tmp_path / "bot"
        (home / ".openclaw").mkdir(parents=True)
        (home / ".openclaw" / "openclaw.json").write_text(json.dumps({}))
        (home / ".openclaw" / "workspace").mkdir(parents=True)
        (home / ".openclaw" / "workspace" / ".env").write_text(
            'export SLACK_BOT_TOKEN="xoxb-with-export"\n'
        )
        view = load_openclaw_view("bot", home)
        assert view.bot_token == "xoxb-with-export"

    def test_comment_lines_ignored(self, tmp_path: Path) -> None:
        from evolve_admin.integrations.slack.oc_config import load_openclaw_view
        home = tmp_path / "bot"
        (home / ".openclaw").mkdir(parents=True)
        (home / ".openclaw" / "openclaw.json").write_text(json.dumps({}))
        (home / ".openclaw" / "workspace").mkdir(parents=True)
        (home / ".openclaw" / "workspace" / ".env").write_text(
            "# SLACK_BOT_TOKEN=xoxb-commented\n"
            "OTHER_VAR=something\n"
        )
        view = load_openclaw_view("bot", home)
        assert view.bot_token is None


class TestRepoLeakRedaction:
    def test_repr_redacts_bot_token(self, tmp_path: Path) -> None:
        """A view's __repr__ must not include the bot token (log-leak vector)."""
        from evolve_admin.integrations.slack.oc_config import load_openclaw_view
        home = _make_bot_home(tmp_path, openclaw_payload=_payload(bot_token="xoxb-SECRET"))
        view = load_openclaw_view("bot", home)
        assert "xoxb-SECRET" not in repr(view)
        # Sanity: the token is actually loaded (otherwise the test is moot)
        assert view.bot_token == "xoxb-SECRET"


class TestSecretRefTokenReading:
    """Token field accepts plaintext OR SecretRef objects (per OC docs)."""

    def test_plain_string_token_reads(self, tmp_path: Path) -> None:
        from evolve_admin.integrations.slack.oc_config import load_openclaw_view
        home = _make_bot_home(tmp_path, openclaw_payload=_payload(bot_token="xoxb-plain"))
        view = load_openclaw_view("team_bot_a", home)
        assert view.bot_token == "xoxb-plain"
        assert view.bot_token_source == "openclaw_channels"

    def test_secretref_env_resolves_from_env_lookup(self, tmp_path: Path) -> None:
        from evolve_admin.integrations.slack.oc_config import resolve_token_value
        payload = {"source": "env", "provider": "default", "id": "SLACK_BOT_TOKEN"}
        token, source = resolve_token_value(
            payload,
            env_lookup=lambda k: "xoxb-from-secretref" if k == "SLACK_BOT_TOKEN" else None,
        )
        assert token == "xoxb-from-secretref"
        assert source == "secretref:env:SLACK_BOT_TOKEN"

    def test_secretref_missing_env_var_is_none(self) -> None:
        from evolve_admin.integrations.slack.oc_config import resolve_token_value
        token, source = resolve_token_value(
            {"source": "env", "id": "ABSENT_VAR"},
            env_lookup=lambda k: None,
        )
        assert token is None
        assert source is None

    def test_unknown_object_shape_is_none(self) -> None:
        from evolve_admin.integrations.slack.oc_config import resolve_token_value
        # source != "env" → not supported yet
        token, source = resolve_token_value({"source": "vault", "id": "x"})
        assert token is None


class TestSlk012EnabledFalse:
    def test_enabled_false_surfaces_info_finding(self, tmp_path: Path) -> None:
        payload = _payload()
        payload["channels"]["slack"]["enabled"] = False
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        client = FakeSlackClient()
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        slk012 = [f for f in result.findings if f.code == "SLK012"]
        assert len(slk012) == 1
        assert slk012[0].severity == "info"
        assert result.slack_enabled is False

    def test_enabled_true_no_finding(self, tmp_path: Path) -> None:
        payload = _payload()
        payload["channels"]["slack"]["enabled"] = True
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        client = FakeSlackClient()
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        assert not any(f.code == "SLK012" for f in result.findings)

    def test_disabled_short_circuits_other_checks(self, tmp_path: Path) -> None:
        """admin_bot-2026-05-28 — a Telegram-only bot with a dormant slack
        block (enabled=false, name-keyed channels, broken token) was
        emitting SLK001/SLK007 FAILs on every deploy. The disabled gate
        must stop the doctor right after SLK012 so the operator isn't
        nagged about config they've explicitly turned off."""
        payload = _payload(
            bot_token="xoxb-broken",
            channels={"project-x": {"requireMention": False}},  # name-keyed → would SLK001
        )
        payload["channels"]["slack"]["enabled"] = False
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        client = FakeSlackClient(
            auth_error=SlackError(code="api_error", detail="invalid_auth"),  # would SLK007 FAIL
        )
        result = run_doctor("admin_bot", bot_home=home, slack_client_factory=lambda _t: client)

        codes = {f.code for f in result.findings}
        assert "SLK012" in codes, "SLK012 must still emit so disabled state is visible"
        for noisy in ("SLK001", "SLK007", "SLK003", "SLK017"):
            assert noisy not in codes, (
                f"{noisy} fired despite slack.enabled=false — gate must short-circuit"
            )


class TestSlk013PolicyEnums:
    def test_unknown_dmPolicy_warns(self, tmp_path: Path) -> None:
        payload = _payload()
        payload["channels"]["slack"]["dmPolicy"] = "bogus_mode"
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        client = FakeSlackClient()
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        slk013 = [f for f in result.findings if f.code == "SLK013"]
        assert any(f.severity == "warn" and "bogus_mode" in f.title for f in slk013)

    def test_open_dmPolicy_without_wildcard_is_fail(self, tmp_path: Path) -> None:
        """Per OC docs: dmPolicy=open requires allowFrom: ["*"]; otherwise silent fail."""
        payload = _payload(dm_allowed=["U0AAA", "U0BBB"])  # no "*"
        payload["channels"]["slack"]["dmPolicy"] = "open"
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        client = FakeSlackClient()
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        slk013 = [f for f in result.findings if f.code == "SLK013"]
        assert any(f.severity == "fail" for f in slk013), (
            "dmPolicy=open without '*' must FAIL per spec"
        )

    def test_open_dmPolicy_with_wildcard_no_fail(self, tmp_path: Path) -> None:
        payload = _payload(dm_allowed=["*"])
        payload["channels"]["slack"]["dmPolicy"] = "open"
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        client = FakeSlackClient()
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        # No SLK013 FAIL — the wildcard satisfies the open-policy requirement
        assert not any(
            f.code == "SLK013" and f.severity == "fail" for f in result.findings
        )

    def test_disabled_dmPolicy_is_info(self, tmp_path: Path) -> None:
        payload = _payload()
        payload["channels"]["slack"]["dmPolicy"] = "disabled"
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        client = FakeSlackClient()
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        slk013 = [f for f in result.findings if f.code == "SLK013"]
        assert any(f.severity == "info" and "DMs to the bot are disabled" in f.title for f in slk013)


class TestSlk015StreamingModeTrap:
    """The team_bot_a-2026-05-14 incident: streaming.mode "partial" in a team
    channel broadcasts internal tool calls to every channel member.
    """

    def _payload_with_streaming(
        self,
        *,
        mode: str | None = "partial",
        native_transport: bool | None = True,
        channels: dict | None = None,
    ) -> dict:
        payload = _payload(channels=channels or {})
        streaming_block: dict = {}
        if mode is not None:
            streaming_block["mode"] = mode
        if native_transport is not None:
            streaming_block["nativeTransport"] = native_transport
        if streaming_block:
            payload["channels"]["slack"]["streaming"] = streaming_block
        return payload

    def test_partial_plus_listens_all_channel_is_fail(self, tmp_path: Path) -> None:
        """Reproduces the team_bot_a-2026-05-14 incident shape exactly."""
        home = _make_bot_home(tmp_path, openclaw_payload=self._payload_with_streaming(
            channels={"G0T79FGSE": {"requireMention": False}},
        ))
        client = FakeSlackClient(member_channels=[{"id": "G0T79FGSE", "name": "ops"}])
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        slk015 = [f for f in result.findings if f.code == "SLK015"]
        assert len(slk015) == 1
        assert slk015[0].severity == "fail"
        # The detail should name the hazard + the recommended fix ("off")
        assert "scaffolding" in slk015[0].detail or "wall" in slk015[0].detail
        assert "\"off\"" in slk015[0].detail

    def test_partial_with_native_transport_mentions_team_bot_a_shape(self, tmp_path: Path) -> None:
        """SLK015's detail surfaces the team_bot_a-incident escalation when
        nativeTransport=true tags along."""
        home = _make_bot_home(tmp_path, openclaw_payload=self._payload_with_streaming(
            mode="partial", native_transport=True,
            channels={"G0T79FGSE": {"requireMention": False}},
        ))
        client = FakeSlackClient(member_channels=[{"id": "G0T79FGSE", "name": "ops"}])
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        slk015 = [f for f in result.findings if f.code == "SLK015"]
        assert any("nativeTransport" in f.detail for f in slk015)

    def test_partial_with_only_mention_only_channels_is_warn(
        self, tmp_path: Path,
    ) -> None:
        home = _make_bot_home(tmp_path, openclaw_payload=self._payload_with_streaming(
            channels={"G0T79FGSE": {"requireMention": True}},
        ))
        client = FakeSlackClient(member_channels=[{"id": "G0T79FGSE", "name": "ops"}])
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        slk015 = [f for f in result.findings if f.code == "SLK015"]
        assert len(slk015) == 1
        assert slk015[0].severity == "warn"

    def test_block_mode_in_listens_all_is_fail(self, tmp_path: Path) -> None:
        """Corrected post-team_bot_a-2026-05-15: block ALSO streams to the channel
        per OC docs ("appends chunked preview updates"). Earlier doctor
        logic wrongly treated block as safe.
        """
        home = _make_bot_home(tmp_path, openclaw_payload=self._payload_with_streaming(
            mode="block", native_transport=False,
            channels={"G0T79FGSE": {"requireMention": False}},
        ))
        client = FakeSlackClient(member_channels=[{"id": "G0T79FGSE", "name": "ops"}])
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        slk015 = [f for f in result.findings if f.code == "SLK015"]
        assert len(slk015) == 1
        assert slk015[0].severity == "fail"
        # The fix recommendation is "off", not "block"
        assert "\"off\"" in slk015[0].detail

    def test_block_mode_in_mention_only_is_warn(self, tmp_path: Path) -> None:
        home = _make_bot_home(tmp_path, openclaw_payload=self._payload_with_streaming(
            mode="block", native_transport=False,
            channels={"G0T79FGSE": {"requireMention": True}},
        ))
        client = FakeSlackClient(member_channels=[{"id": "G0T79FGSE", "name": "ops"}])
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        slk015 = [f for f in result.findings if f.code == "SLK015"]
        assert len(slk015) == 1
        assert slk015[0].severity == "warn"

    def test_off_mode_no_finding(self, tmp_path: Path) -> None:
        home = _make_bot_home(tmp_path, openclaw_payload=self._payload_with_streaming(
            mode="off",
            channels={"G0T79FGSE": {"requireMention": False}},
        ))
        client = FakeSlackClient(member_channels=[{"id": "G0T79FGSE", "name": "ops"}])
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        assert not any(f.code == "SLK015" for f in result.findings)

    def test_unset_streaming_mode_surfaces_info(self, tmp_path: Path) -> None:
        """Absent streaming.mode → OC default applies. We don't know
        the default and OC has flipped defaults silently before, so
        surface as INFO when the bot has any channels."""
        home = _make_bot_home(tmp_path, openclaw_payload=_payload(
            channels={"G0T79FGSE": {"requireMention": False}},
        ))
        client = FakeSlackClient(member_channels=[{"id": "G0T79FGSE", "name": "ops"}])
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        slk015_info = [
            f for f in result.findings if f.code == "SLK015" and f.severity == "info"
        ]
        assert len(slk015_info) == 1

    def test_unknown_mode_value_warns(self, tmp_path: Path) -> None:
        home = _make_bot_home(tmp_path, openclaw_payload=self._payload_with_streaming(
            mode="exuberant",
            channels={"G0T79FGSE": {"requireMention": False}},
        ))
        client = FakeSlackClient(member_channels=[{"id": "G0T79FGSE", "name": "ops"}])
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        slk015 = [f for f in result.findings if f.code == "SLK015"]
        assert len(slk015) == 1
        assert slk015[0].severity == "warn"
        assert "exuberant" in slk015[0].title

    def test_dm_only_bot_no_finding(self, tmp_path: Path) -> None:
        """A bot with zero channel entries (DM-only) is fine on `partial`."""
        home = _make_bot_home(tmp_path, openclaw_payload=self._payload_with_streaming(
            mode="partial",
            channels={},
        ))
        client = FakeSlackClient(member_channels=[])
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        assert not any(f.code == "SLK015" for f in result.findings)


class TestSlk016CommandTextLeak:
    """The team_bot_a-2026-05-15 incident's second hazard: even with
    streaming.mode set, `progress.commandText: "raw"` (the default)
    surfaces the literal `command run python3 …` text into the
    streamed message body. Independent check from SLK015 since each
    has its own fix.
    """

    def _payload(
        self,
        *,
        mode: str = "block",
        command_text: str | None = None,
        channels: dict | None = None,
    ) -> dict:
        # `channels if not None` instead of `channels or default` — an
        # explicit empty dict means "DM-only bot" and must override the
        # default. The `or` short-circuit treats `{}` as falsy.
        ch = channels if channels is not None else {"G0T79FGSE": {"requireMention": False}}
        payload = _payload(channels=ch)
        streaming: dict = {"mode": mode, "nativeTransport": False}
        if command_text is not None:
            streaming["progress"] = {"commandText": command_text}
        payload["channels"]["slack"]["streaming"] = streaming
        return payload

    def test_command_text_raw_in_listens_all_warns(self, tmp_path: Path) -> None:
        home = _make_bot_home(tmp_path, openclaw_payload=self._payload(
            mode="block", command_text="raw",
        ))
        client = FakeSlackClient(member_channels=[{"id": "G0T79FGSE", "name": "ops"}])
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        slk016 = [f for f in result.findings if f.code == "SLK016"]
        assert len(slk016) == 1
        assert slk016[0].severity == "warn"
        # The fix names "status" as the safe value
        assert "status" in slk016[0].detail

    def test_command_text_unset_warns_in_team_channel(self, tmp_path: Path) -> None:
        """Per docs, the default is "raw" — so unset is the same hazard."""
        home = _make_bot_home(tmp_path, openclaw_payload=self._payload(
            mode="block", command_text=None,
        ))
        client = FakeSlackClient(member_channels=[{"id": "G0T79FGSE", "name": "ops"}])
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        slk016 = [f for f in result.findings if f.code == "SLK016"]
        assert len(slk016) == 1
        assert "defaults to" in slk016[0].title  # mentions the default in the title

    def test_command_text_status_no_finding(self, tmp_path: Path) -> None:
        home = _make_bot_home(tmp_path, openclaw_payload=self._payload(
            mode="block", command_text="status",
        ))
        client = FakeSlackClient(member_channels=[{"id": "G0T79FGSE", "name": "ops"}])
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        assert not any(f.code == "SLK016" for f in result.findings)

    def test_streaming_off_supersedes_command_text(self, tmp_path: Path) -> None:
        """When mode=off no commands are rendered anywhere; commandText
        doesn't matter. Don't double-fire."""
        home = _make_bot_home(tmp_path, openclaw_payload=self._payload(
            mode="off", command_text="raw",
        ))
        client = FakeSlackClient(member_channels=[{"id": "G0T79FGSE", "name": "ops"}])
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        # Neither SLK015 nor SLK016 should fire — operator picked the
        # silent path; raw commandText is moot.
        assert not any(f.code in {"SLK015", "SLK016"} for f in result.findings)

    def test_dm_only_bot_no_finding(self, tmp_path: Path) -> None:
        home = _make_bot_home(tmp_path, openclaw_payload=self._payload(
            mode="block", command_text="raw", channels={},
        ))
        client = FakeSlackClient(member_channels=[])
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        assert not any(f.code == "SLK016" for f in result.findings)


class TestScopeBundleEvaluation:
    """OAuth-scope checklist (the "incremental scopes" Plex-test win)."""

    def test_full_scope_set_enables_messaging_bundles(self, tmp_path: Path) -> None:
        payload = _payload()
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        # Mimic the Slack API returning the canonical messaging scope set
        client = FakeSlackClient(auth_response={
            "user_id": "U0BOT", "user": "team_bot_a",
            "team_id": "T123", "team": "Test", "bot_id": "B1",
            "_scopes": [
                "app_mentions:read", "chat:write",
                "im:history", "im:read", "im:write",
                "channels:history", "channels:read",
                "reactions:write", "users:read",
            ],
        })
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        assert result.oauth_scopes
        enabled_names = {
            b.bundle.name for b in result.feature_bundles if b.enabled
        }
        assert "Respond to @-mentions" in enabled_names
        assert "Send and receive direct messages" in enabled_names
        assert 'Show "thinking" via emoji reaction' in enabled_names

    def test_missing_reactions_write_disables_thinking_bundle(self, tmp_path: Path) -> None:
        payload = _payload()
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        # Has every messaging scope EXCEPT reactions:write
        client = FakeSlackClient(auth_response={
            "user_id": "U0BOT", "user": "team_bot_a", "team_id": "T123",
            "team": "Test", "bot_id": "B1",
            "_scopes": [
                "app_mentions:read", "chat:write",
                "im:history", "im:read", "im:write",
                "channels:history", "channels:read",
            ],
        })
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        thinking = next(
            b for b in result.feature_bundles
            if b.bundle.key == "thinking_reaction"
        )
        assert thinking.enabled is False
        assert "reactions:write" in thinking.missing

    def test_elevated_scopes_surfaced(self, tmp_path: Path) -> None:
        payload = _payload()
        home = _make_bot_home(tmp_path, openclaw_payload=payload)
        client = FakeSlackClient(auth_response={
            "user_id": "U0BOT", "user": "team_bot_a", "team_id": "T123",
            "team": "Test", "bot_id": "B1",
            "_scopes": ["chat:write", "channels:manage", "usergroups:write"],
        })
        result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
        assert "channels:manage" in result.elevated_scopes_granted
        assert "usergroups:write" in result.elevated_scopes_granted


def test_clean_config_no_findings(tmp_path: Path) -> None:
    payload = _payload(
        channels={
            "G0T79FGSE": {"requireMention": False},
            "C0AL2GDUA7J": {"requireMention": True},
        },
        visible_replies="automatic",
    )
    home = _make_bot_home(tmp_path, openclaw_payload=payload)
    client = FakeSlackClient(member_channels=[
        {"id": "G0T79FGSE", "name": "management"},
        {"id": "C0AL2GDUA7J", "name": "project-x"},
    ])
    result = run_doctor("team_bot_a", bot_home=home, slack_client_factory=lambda _t: client)
    # SLK008 (workspace info) fires at INFO unconditionally on a successful
    # auth.test — that's allowed. No FAIL or WARN is the contract.
    assert not any(f.severity in {"fail", "warn"} for f in result.findings), (
        f"unexpected findings: {[f.code for f in result.findings if f.severity in {'fail','warn'}]}"
    )
