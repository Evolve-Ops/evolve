"""Tests for the investigation extensions in audit_dispatch.

Workstream C — covers ``request_investigation``,
``latest_investigation_for_user``, ``count_investigations_in_window``,
and ``flag_investigation_unresolved``.

Same pattern as ``test_audit_dispatch.py``: re-point the bot-side path
resolver into a temp tree so we can write/read trail files without
needing a real bot user.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import audit_dispatch  # noqa: E402


@pytest.fixture
def tmp_inbox_root(tmp_path: Path, monkeypatch):
    """Redirect bot-side path resolvers (inbox, trail, outbox) into tmp_path."""
    def _inbox(bot_user: str) -> Path:
        return (
            tmp_path / "Users" / bot_user
            / ".openclaw" / "workspace" / "evolve" / "audit_inbox"
        )

    def _trail(bot_user: str) -> Path:
        return (
            tmp_path / "Users" / bot_user
            / ".openclaw" / "workspace" / "evolve"
            / "investigations" / "trail.jsonl"
        )

    def _outbox(bot_user: str) -> Path:
        return (
            tmp_path / "Users" / bot_user
            / ".openclaw" / "workspace" / "evolve" / "audit_outbox"
        )

    monkeypatch.setattr(audit_dispatch, "_audit_inbox_dir", _inbox)
    monkeypatch.setattr(audit_dispatch, "_investigations_trail_path", _trail)
    monkeypatch.setattr(audit_dispatch, "_investigations_outbox_dir", _outbox)
    return tmp_path


def _write_trail_entry(
    tmp_root: Path, bot_user: str, entry: dict,
) -> Path:
    """Append one trail entry to investigations/trail.jsonl."""
    trail_dir = (
        tmp_root / "Users" / bot_user
        / ".openclaw" / "workspace" / "evolve" / "investigations"
    )
    trail_dir.mkdir(parents=True, exist_ok=True)
    trail = trail_dir / "trail.jsonl"
    with trail.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")
    return trail


# ── request_investigation ───────────────────────────────────────────────────


def test_request_investigation_writes_inbox_file(
    tmp_inbox_root, monkeypatch,
) -> None:
    """Basic shape: inbox file lands with expected fields, kick fires."""
    monkeypatch.setattr(audit_dispatch, "_kick_runner", lambda u, r: (True, ""))
    result = audit_dispatch.request_investigation(
        bot_id="team_bot_a", bot_user="team_bot_a",
        user_description="morning briefing didn't arrive today",
        requesting_user="pod:pod_admin_user",
        kick=False,
    )
    assert result.ok
    assert result.investigation_id.startswith("inv-")
    inbox_dir = (
        tmp_inbox_root / "Users/team_bot_a/.openclaw/workspace/evolve/audit_inbox"
    )
    files = list(inbox_dir.glob("investigation-*.json"))
    assert len(files) == 1
    body = json.loads(files[0].read_text())
    assert body["kind"] == "investigation"
    assert body["user_description"] == "morning briefing didn't arrive today"
    assert body["requesting_user"] == "pod:pod_admin_user"
    assert body["investigation_id"] == result.investigation_id


def test_request_investigation_empty_description_fails(
    tmp_inbox_root, monkeypatch,
) -> None:
    monkeypatch.setattr(audit_dispatch, "_kick_runner", lambda u, r: (True, ""))
    result = audit_dispatch.request_investigation(
        bot_id="team_bot_a", bot_user="team_bot_a",
        user_description="   ",
        requesting_user="pod:pod_admin_user",
        kick=False,
    )
    assert result.ok is False
    assert "required" in result.error.lower()


def test_request_investigation_kick_called(
    tmp_inbox_root, monkeypatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def _fake_kick(user, rid):
        calls.append((user, rid))
        return True, ""

    monkeypatch.setattr(audit_dispatch, "_kick_runner", _fake_kick)
    result = audit_dispatch.request_investigation(
        bot_id="team_bot_a", bot_user="team_bot_a",
        user_description="thing failed",
        requesting_user="pod:pod_admin_user",
        kick=True,
    )
    assert result.kicked is True
    assert calls == [("team_bot_a", result.investigation_id)]


# ── latest_investigation_for_user ───────────────────────────────────────────


def test_latest_investigation_returns_none_when_no_trail(
    tmp_inbox_root,
) -> None:
    out = audit_dispatch.latest_investigation_for_user("team_bot_a", "pod:pod_admin_user")
    assert out is None


def test_latest_investigation_returns_last_matching(
    tmp_inbox_root,
) -> None:
    _write_trail_entry(tmp_inbox_root, "team_bot_a", {
        "ts": "2026-05-15T10:00:00Z",
        "kind": "investigation",
        "investigation_id": "inv-aaa",
        "user_description": "older one",
        "requesting_user": "pod:pod_admin_user",
    })
    _write_trail_entry(tmp_inbox_root, "team_bot_a", {
        "ts": "2026-05-15T11:00:00Z",
        "kind": "investigation",
        "investigation_id": "inv-bbb",
        "user_description": "someone else's",
        "requesting_user": "pod:someone",
    })
    _write_trail_entry(tmp_inbox_root, "team_bot_a", {
        "ts": "2026-05-15T12:00:00Z",
        "kind": "investigation",
        "investigation_id": "inv-ccc",
        "user_description": "latest one",
        "requesting_user": "pod:pod_admin_user",
    })
    out = audit_dispatch.latest_investigation_for_user("team_bot_a", "pod:pod_admin_user")
    assert out is not None
    assert out["investigation_id"] == "inv-ccc"
    assert out["user_description"] == "latest one"


# ── count_investigations_in_window ──────────────────────────────────────────


def test_count_investigations_zero_when_no_trail(tmp_inbox_root) -> None:
    n = audit_dispatch.count_investigations_in_window(
        "team_bot_a", "pod:pod_admin_user", window_seconds=3600,
    )
    assert n == 0


def test_count_investigations_respects_window(tmp_inbox_root) -> None:
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for ts, iid in [(recent, "inv-1"), (recent, "inv-2"), (old, "inv-3")]:
        _write_trail_entry(tmp_inbox_root, "team_bot_a", {
            "ts": ts, "kind": "investigation",
            "investigation_id": iid,
            "user_description": "x",
            "requesting_user": "pod:pod_admin_user",
        })
    n = audit_dispatch.count_investigations_in_window(
        "team_bot_a", "pod:pod_admin_user", window_seconds=3600,
    )
    assert n == 2


# ── flag_investigation_unresolved ───────────────────────────────────────────


def test_flag_unresolved_fails_when_no_prior(tmp_inbox_root) -> None:
    ok, err, inv_id = audit_dispatch.flag_investigation_unresolved(
        bot_id="team_bot_a", bot_user="team_bot_a",
        requesting_user="pod:pod_admin_user",
    )
    assert ok is False
    assert "no recent investigation" in err.lower()
    assert inv_id == ""


def test_flag_unresolved_writes_outbox_record(tmp_inbox_root) -> None:
    """A prior trail entry → outbox record of kind investigation_unresolved.

    The tmp_inbox_root fixture redirects the bot-side path resolvers
    (trail + outbox) into a temp tree so we can read what the function
    writes.
    """
    _write_trail_entry(tmp_inbox_root, "team_bot_a", {
        "ts": "2026-05-15T12:00:00Z",
        "kind": "investigation",
        "investigation_id": "inv-prior",
        "user_description": "morning briefing didn't arrive",
        "requesting_user": "pod:pod_admin_user",
        "triage_candidates": [{"element_type": "app", "element_id": "morning-briefing"}],
        "chosen_candidate": {"element_type": "app", "element_id": "morning-briefing"},
        "evidence": ["scripts/briefing.py:42"],
        "diagnosis": None,
        "confidence": "low",
    })

    ok, err, inv_id = audit_dispatch.flag_investigation_unresolved(
        bot_id="team_bot_a", bot_user="team_bot_a",
        requesting_user="pod:pod_admin_user",
    )
    assert ok is True, err
    assert inv_id == "inv-prior"

    outbox_dir = (
        tmp_inbox_root / "Users/team_bot_a/.openclaw/workspace/evolve/audit_outbox"
    )
    files = list(outbox_dir.glob("inv-flag-*.json"))
    assert len(files) == 1
    body = json.loads(files[0].read_text())
    assert body["kind"] == "investigation_unresolved"
    assert body["investigation_id"] == "inv-prior"
    assert body["requesting_user"] == "pod:pod_admin_user"
    assert body["user_description"] == "morning briefing didn't arrive"
    assert body["chosen_candidate"]["element_id"] == "morning-briefing"
