"""Tests for evolve_admin.integrations.slack.directory.

Covers:
- Refresh path (mocked Slack client → write JSON → reload, round-trip)
- Filtering (skip bots, keep deactivated, sort admins to top)
- Email-scope conditional (no email column when scope is missing)
- Staleness check (SLK017's underlying primitive)
- Markdown render shape + length cap
- Disambiguation rule named explicitly in the preamble (the team_bot_a-2026-05-15 fix)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from evolve_admin.integrations.slack.directory import (
    DEFAULT_STALE_AFTER_HOURS,
    DIRECTORY_FILENAME,
    INJECTION_CHAR_CAP,
    UserRecord,
    WorkspaceDirectory,
    directory_age_hours,
    directory_path,
    is_stale,
    load_directory,
    refresh_workspace_directory,
    render_directory_markdown,
    save_directory,
)
from evolve_admin.integrations.slack.slack_client import SlackError


# ─────────────────────────────────────────────────────────────────────────────
# Stubs
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class FakeSlackClient:
    """Stand-in for SlackClient. Only the methods directory.refresh uses."""
    auth_response: dict | None = field(default_factory=lambda: {
        "user": "team_bot_a", "user_id": "U0BOT", "team": "Acme", "team_id": "T1",
        "_scopes": ["users:read"],
    })
    auth_error: SlackError | None = None
    members: list[dict] = field(default_factory=list)
    members_error: SlackError | None = None

    def auth_test(self):
        if self.auth_error is not None:
            return None, self.auth_error
        return self.auth_response, None

    def users_list(self, **_):
        if self.members_error is not None:
            return None, self.members_error
        return list(self.members), None


def _user(
    uid: str,
    *,
    name: str | None = None,
    real_name: str | None = None,
    display_name: str | None = None,
    email: str | None = None,
    is_admin: bool = False,
    is_owner: bool = False,
    is_bot: bool = False,
    deleted: bool = False,
) -> dict:
    """Build the shape Slack's users.list returns."""
    return {
        "id": uid,
        "name": name,
        "real_name": real_name,
        "is_admin": is_admin,
        "is_owner": is_owner,
        "is_bot": is_bot,
        "deleted": deleted,
        "tz": "America/Los_Angeles",
        "profile": {
            "real_name": real_name,
            "display_name": display_name,
            "email": email,
            "title": None,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Refresh round-trips
# ─────────────────────────────────────────────────────────────────────────────


class TestRefreshHappyPath:
    def test_refresh_writes_directory_filtered_and_sorted(
        self, tmp_path: Path,
    ) -> None:
        members = [
            _user("U0AAA", name="alice", real_name="Alice Member"),
            _user("U0BOTAPP", name="someapp", real_name="App Bot", is_bot=True),
            _user("U0OWN", name="pod_admin", real_name="Pod_admin Alden", is_owner=True, is_admin=True),
            _user("U0DEAD", name="ghost", real_name="Former Member", deleted=True),
        ]
        client = FakeSlackClient(members=members)
        result = refresh_workspace_directory(
            "team_bot_a", bot_token="xoxb-test", shared_dir=tmp_path,
            slack_client=client,
        )
        assert result.ok
        assert result.user_count == 3  # bot filtered out
        assert result.team_name == "Acme"

        directory = load_directory(tmp_path, "team_bot_a")
        assert directory is not None
        # Admins sort to top
        assert directory.users[0].id == "U0OWN"
        # Bots filtered
        assert not any(u.is_bot for u in directory.users)
        # Deactivated kept (so historical messages still resolve)
        ids = [u.id for u in directory.users]
        assert "U0DEAD" in ids
        # role_label for deactivated
        dead = next(u for u in directory.users if u.id == "U0DEAD")
        assert dead.role_label() == "deactivated"

    def test_refresh_captures_email_scope_flag(self, tmp_path: Path) -> None:
        client = FakeSlackClient(
            auth_response={
                "team": "Acme", "team_id": "T1", "user": "team_bot_a", "user_id": "U0BOT",
                "_scopes": ["users:read", "users:read.email"],
            },
            members=[_user("U0AAA", name="alice", real_name="Alice", email="alice@example.com")],
        )
        result = refresh_workspace_directory(
            "team_bot_a", bot_token="xoxb-test", shared_dir=tmp_path,
            slack_client=client,
        )
        assert result.ok
        assert result.users_read_email_scope is True
        directory = load_directory(tmp_path, "team_bot_a")
        assert directory is not None
        assert directory.users_read_email_scope is True
        assert directory.users[0].email == "alice@example.com"

    def test_refresh_without_email_scope_skips_email_column(
        self, tmp_path: Path,
    ) -> None:
        # auth.test scopes don't include users:read.email
        client = FakeSlackClient(
            members=[_user("U0AAA", name="alice", real_name="Alice")],
        )
        result = refresh_workspace_directory(
            "team_bot_a", bot_token="xoxb-test", shared_dir=tmp_path,
            slack_client=client,
        )
        assert result.ok
        assert result.users_read_email_scope is False


class TestRefreshErrorPaths:
    def test_auth_failure_no_write(self, tmp_path: Path) -> None:
        client = FakeSlackClient(
            auth_error=SlackError(code="api_error", detail="", slack_error="invalid_auth"),
        )
        result = refresh_workspace_directory(
            "team_bot_a", bot_token="xoxb-test", shared_dir=tmp_path,
            slack_client=client,
        )
        assert result.ok is False
        assert "auth_test_failed" in (result.error or "")
        assert load_directory(tmp_path, "team_bot_a") is None

    def test_users_list_failure_no_write(self, tmp_path: Path) -> None:
        client = FakeSlackClient(
            members_error=SlackError(code="api_error", detail="", slack_error="missing_scope"),
        )
        result = refresh_workspace_directory(
            "team_bot_a", bot_token="xoxb-test", shared_dir=tmp_path,
            slack_client=client,
        )
        assert result.ok is False
        assert "users_list_failed" in (result.error or "")
        assert load_directory(tmp_path, "team_bot_a") is None

    def test_empty_token_returns_invalid(self, tmp_path: Path) -> None:
        result = refresh_workspace_directory(
            "team_bot_a", bot_token="", shared_dir=tmp_path,
        )
        assert result.ok is False
        assert "invalid_token" in (result.error or "")


# ─────────────────────────────────────────────────────────────────────────────
# Markdown render
# ─────────────────────────────────────────────────────────────────────────────


class TestRenderMarkdown:
    def _directory(
        self, *, users: list[UserRecord] | None = None, email_scope: bool = False,
    ) -> WorkspaceDirectory:
        return WorkspaceDirectory(
            bot_id="team_bot_a",
            team_id="T1",
            team_name="Acme",
            last_refreshed_at="2026-05-15T15:30:00Z",
            users_read_email_scope=email_scope,
            users=users or [
                UserRecord(id="U0PLKKXV0", name="pod_admin", real_name="Pod_admin Alden",
                           is_admin=True, is_owner=True),
                UserRecord(id="U07CH6GLF8R", name="andi", real_name="Andi Smith"),
            ],
        )

    def test_render_includes_disambiguation_rule_in_preamble(self) -> None:
        """The whole point of the directory — make the rule explicit."""
        out = render_directory_markdown(self._directory())
        assert "Slack ID" in out
        assert "pod_admin" in out
        assert "Pod_admin Alden" in out
        assert "U0PLKKXV0" in out
        # The team_bot_a-2026-05-15 rule must be stated, not implied
        assert "same person" in out.lower() or "same individual" in out.lower()

    def test_render_omits_email_column_when_scope_absent(self) -> None:
        out = render_directory_markdown(self._directory(email_scope=False))
        # The email column header doesn't appear
        assert "| email |" not in out
        assert "email" not in out.split("---")[1] if "---" in out else True

    def test_render_includes_email_column_when_scope_present(self) -> None:
        out = render_directory_markdown(self._directory(
            users=[UserRecord(id="U0A", name="a", real_name="A", email="a@x.com")],
            email_scope=True,
        ))
        assert "email" in out
        assert "a@x.com" in out

    def test_render_empty_directory_is_empty_string(self) -> None:
        empty = WorkspaceDirectory(bot_id="team_bot_a", users=[])
        assert render_directory_markdown(empty) == ""

    def test_render_truncates_huge_directory_at_row_boundary(self) -> None:
        # Synthesize 500 users to force truncation
        users = [
            UserRecord(id=f"U{i:08d}", name=f"user{i}", real_name=f"User Number {i}")
            for i in range(500)
        ]
        directory = WorkspaceDirectory(
            bot_id="team_bot_a", team_id="T1", team_name="Acme",
            last_refreshed_at="2026-05-15T15:30:00Z", users=users,
        )
        out = render_directory_markdown(directory, cap_chars=INJECTION_CHAR_CAP)
        # Body stays under the cap
        assert len(out) <= INJECTION_CHAR_CAP + 500  # the tail note adds a bit
        # Truncation note present
        assert "truncated for systemAppend" in out
        # No row ends mid-pipe (clean boundary). Find the header row's
        # pipe count and compare every data row against it.
        lines = out.splitlines()
        header_line = next(l for l in lines if l.startswith("| Slack ID"))
        expected_pipes = header_line.count("|")
        table_lines = [l for l in lines if l.startswith("| U")]
        assert len(table_lines) > 5, "should have kept some rows after truncation"
        for line in table_lines:
            assert line.count("|") == expected_pipes, f"Truncated row: {line!r}"


# ─────────────────────────────────────────────────────────────────────────────
# Staleness
# ─────────────────────────────────────────────────────────────────────────────


class TestStaleness:
    def test_fresh_directory_not_stale(self) -> None:
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ",
        )
        d = WorkspaceDirectory(bot_id="team_bot_a", last_refreshed_at=recent)
        assert is_stale(d) is False
        age = directory_age_hours(d)
        assert age is not None and 0 < age < 2

    def test_old_directory_is_stale(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime(
            "%Y-%m-%dT%H:%M:%SZ",
        )
        d = WorkspaceDirectory(bot_id="team_bot_a", last_refreshed_at=old)
        assert is_stale(d) is True

    def test_missing_timestamp_is_stale(self) -> None:
        d = WorkspaceDirectory(bot_id="team_bot_a", last_refreshed_at="")
        assert is_stale(d) is True

    def test_custom_stale_threshold(self) -> None:
        recent = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(
            "%Y-%m-%dT%H:%M:%SZ",
        )
        d = WorkspaceDirectory(bot_id="team_bot_a", last_refreshed_at=recent)
        # Stricter threshold: 1h
        assert is_stale(d, stale_after_hours=1) is True
        # Looser: default 24h → fine
        assert is_stale(d, stale_after_hours=24) is False


# ─────────────────────────────────────────────────────────────────────────────
# Storage round-trip
# ─────────────────────────────────────────────────────────────────────────────


class TestStorage:
    def test_save_then_load_round_trips(self, tmp_path: Path) -> None:
        d = WorkspaceDirectory(
            bot_id="team_bot_a",
            team_id="T1", team_name="Acme",
            last_refreshed_at="2026-05-15T15:30:00Z",
            users_read_email_scope=True,
            users=[
                UserRecord(id="U0A", name="alice", real_name="Alice", email="a@x.com"),
            ],
        )
        save_directory(tmp_path, d)
        loaded = load_directory(tmp_path, "team_bot_a")
        assert loaded is not None
        assert loaded.team_name == "Acme"
        assert loaded.users_read_email_scope is True
        assert loaded.users[0].email == "a@x.com"

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert load_directory(tmp_path, "ghost") is None

    def test_malformed_json_raises(self, tmp_path: Path) -> None:
        p = directory_path(tmp_path, "team_bot_a")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{ not valid json")
        with pytest.raises(ValueError, match="not valid JSON"):
            load_directory(tmp_path, "team_bot_a")

    def test_save_creates_bot_subdir(self, tmp_path: Path) -> None:
        # bots/team_bot_a/ doesn't exist yet
        assert not (tmp_path / "bots" / "team_bot_a").exists()
        save_directory(tmp_path, WorkspaceDirectory(
            bot_id="team_bot_a", users=[UserRecord(id="U0A")],
        ))
        assert (tmp_path / "bots" / "team_bot_a" / DIRECTORY_FILENAME).exists()
