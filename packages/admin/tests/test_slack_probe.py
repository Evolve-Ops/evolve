"""Tests for evolve_admin.integrations.slack.probe.

Covers the probe → Signal-store mirroring contract. Uses a stub
``signals_module`` so no real Signal store is touched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from evolve_admin.integrations.slack.probe import PRODUCER, run_probe


# ─────────────────────────────────────────────────────────────────────────────
# Stubs
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class FakeSignalsModule:
    observed: list[dict] = field(default_factory=list)
    swept: list[str] = field(default_factory=list)
    last_kept_signatures: set = field(default_factory=set)

    def observe(self, _shared_dir, **kwargs):
        self.observed.append(kwargs)
        return type("S", (), {"id": f"sig-{len(self.observed)}"})()

    def sweep_resolve(self, _shared_dir, *, producer, kept_signatures, reason):
        assert producer == PRODUCER
        self.last_kept_signatures = set(kept_signatures)
        return list(self.swept)


def _fake_make_signature(producer: str, type_: str, scope_key: str) -> str:
    return f"{producer}:{type_}:{scope_key}"


def _write_bot_openclaw(home: Path, payload: dict) -> None:
    (home / ".openclaw").mkdir(parents=True, exist_ok=True)
    (home / ".openclaw" / "openclaw.json").write_text(json.dumps(payload))


def _slack_factory(
    member_channels: list[dict],
    *,
    auth_ok: bool = True,
    mpim_channels: list[dict] | None = None,
):
    """Return a slack_client_factory that produces FakeSlackClients."""
    from .test_slack_doctor import FakeSlackClient
    from evolve_admin.integrations.slack.slack_client import SlackError

    def factory(_token):
        if auth_ok:
            return FakeSlackClient(
                member_channels=member_channels,
                mpim_channels=list(mpim_channels or []),
            )
        return FakeSlackClient(
            auth_error=SlackError(code="api_error", detail="", slack_error="invalid_auth"),
        )
    return factory


# ─────────────────────────────────────────────────────────────────────────────
# Signal emission
# ─────────────────────────────────────────────────────────────────────────────


def test_name_keyed_channel_emits_signal(tmp_path: Path) -> None:
    home = tmp_path / "team_bot_a"
    _write_bot_openclaw(home, {
        "channels": {
            "slack": {
                "botToken": "xoxb-test",
                "groupPolicy": "allowlist",
                "channels": {"project-x": {"requireMention": False}},
            },
        },
        "messages": {"groupChat": {"visibleReplies": "automatic"}},
    })

    signals = FakeSignalsModule()
    report = run_probe(
        shared_dir=tmp_path,
        bot_homes={"team_bot_a": home},
        slack_client_factory=_slack_factory([]),
        signals_module=signals,
        make_signature=_fake_make_signature,
    )

    assert report.bots_scanned == 1
    types_emitted = {s["type"] for s in signals.observed}
    assert "name_keyed_channel" in types_emitted
    # Severity preserved from doctor finding
    name_keyed = [s for s in signals.observed if s["type"] == "name_keyed_channel"][0]
    assert name_keyed["severity"] == "alert"
    assert name_keyed["flavor"] == "maintenance"
    assert name_keyed["bot_id"] == "team_bot_a"


def test_info_findings_do_not_emit_signals(tmp_path: Path) -> None:
    """SLK008 (workspace info) fires unconditionally on auth success — must not signal."""
    home = tmp_path / "team_bot_a"
    _write_bot_openclaw(home, {
        "channels": {
            "slack": {
                "botToken": "xoxb-test",
                "groupPolicy": "allowlist",
                "channels": {"G0T79FGSE": {"requireMention": False}},
                "streaming": {"mode": "off"},
            },
        },
        "messages": {"groupChat": {"visibleReplies": "automatic"}},
    })
    # SLK017 would otherwise fire WARN (no slack-directory.json) — write
    # a fresh one so this test stays focused on INFO-only behavior.
    from datetime import datetime, timezone
    from evolve_admin.integrations.slack.directory import (
        UserRecord, WorkspaceDirectory, save_directory,
    )
    save_directory(tmp_path, WorkspaceDirectory(
        bot_id="team_bot_a", team_id="T1", team_name="Acme",
        last_refreshed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        users=[UserRecord(id="U0A", name="alice", real_name="Alice")],
    ))

    signals = FakeSignalsModule()
    run_probe(
        shared_dir=tmp_path,
        bot_homes={"team_bot_a": home},
        slack_client_factory=_slack_factory(
            [{"id": "G0T79FGSE", "name": "management"}],
        ),
        signals_module=signals,
        make_signature=_fake_make_signature,
    )
    assert signals.observed == []  # nothing actionable; pure-INFO run


def test_group_dm_in_allowlist_emits_slk019_signal(tmp_path: Path) -> None:
    """A group-DM ID the bot participates in, listed in the channel
    allowlist, must surface as a non-blocking SLK019 WARN Signal (not a
    blocking SLK002 bot_not_member alert)."""
    home = tmp_path / "team_bot_a"
    _write_bot_openclaw(home, {
        "channels": {
            "slack": {
                "botToken": "xoxb-test",
                "groupPolicy": "allowlist",
                "channels": {"C0B7PDLM2PJ": {"requireMention": False}},
                "streaming": {"mode": "off"},
            },
        },
        "messages": {"groupChat": {"visibleReplies": "automatic"}},
    })
    # Fresh directory so SLK017 stays quiet and the run is focused.
    from datetime import datetime, timezone
    from evolve_admin.integrations.slack.directory import (
        UserRecord, WorkspaceDirectory, save_directory,
    )
    save_directory(tmp_path, WorkspaceDirectory(
        bot_id="team_bot_a", team_id="T1", team_name="Acme",
        last_refreshed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        users=[UserRecord(id="U0A", name="alice", real_name="Alice")],
    ))

    signals = FakeSignalsModule()
    run_probe(
        shared_dir=tmp_path,
        bot_homes={"team_bot_a": home},
        slack_client_factory=_slack_factory(
            [],  # no public/private channels
            mpim_channels=[{"id": "C0B7PDLM2PJ", "name": "mpdm-a--b--c-1"}],
        ),
        signals_module=signals,
        make_signature=_fake_make_signature,
    )

    types_emitted = {s["type"] for s in signals.observed}
    assert "group_dm_in_channel_allowlist" in types_emitted
    assert "bot_not_member" not in types_emitted  # SLK002 suppressed
    slk019 = [s for s in signals.observed if s["type"] == "group_dm_in_channel_allowlist"][0]
    assert slk019["severity"] == "warn"
    assert slk019["flavor"] == "maintenance"


def test_sweep_called_with_kept_signatures(tmp_path: Path) -> None:
    """The probe must always call sweep_resolve so cleared conditions auto-archive."""
    home = tmp_path / "team_bot_a"
    _write_bot_openclaw(home, {
        "channels": {
            "slack": {
                "botToken": "xoxb-test",
                "groupPolicy": "allowlist",
                "channels": {"project-x": {"requireMention": False}},
            },
        },
        "messages": {"groupChat": {"visibleReplies": "automatic"}},
    })

    signals = FakeSignalsModule()
    run_probe(
        shared_dir=tmp_path,
        bot_homes={"team_bot_a": home},
        slack_client_factory=_slack_factory([]),
        signals_module=signals,
        make_signature=_fake_make_signature,
    )
    # Sweep was called with exactly the signatures observe() emitted
    emitted_sigs = {
        _fake_make_signature(PRODUCER, s["type"],
                             f"{s['bot_id']}:{s['details'].get('channel_id') or s['details'].get('user_id') or ''}".rstrip(":"))
        for s in signals.observed
    }
    assert signals.last_kept_signatures == emitted_sigs


def test_doctor_keeps_scanning_after_individual_bot_failure(tmp_path: Path) -> None:
    """An unreadable openclaw.json on one bot must not stop the probe.

    The doctor's read path returns ``(None, error)`` instead of raising,
    so this scenario produces an SLK000 WARN on the bad bot rather
    than an exception. Either way the probe must continue to the good
    bot and emit its findings.
    """
    home1 = tmp_path / "bad"
    (home1 / ".openclaw").mkdir(parents=True)
    home2 = tmp_path / "good"
    _write_bot_openclaw(home2, {
        "channels": {
            "slack": {
                "botToken": "xoxb-test",
                "groupPolicy": "allowlist",
                "channels": {"project-x": {"requireMention": False}},  # SLK001
            },
        },
        "messages": {"groupChat": {"visibleReplies": "automatic"}},
    })
    # Sabotage one bot by making openclaw.json a directory (read fails)
    (home1 / ".openclaw" / "openclaw.json").mkdir()

    signals = FakeSignalsModule()
    report = run_probe(
        shared_dir=tmp_path,
        bot_homes={"bad": home1, "good": home2},
        slack_client_factory=_slack_factory([]),
        signals_module=signals,
        make_signature=_fake_make_signature,
    )
    # Both bots were attempted.
    assert report.bots_scanned == 2
    # The good bot's name_keyed_channel Signal landed despite the bad bot.
    types_emitted = {s["type"] for s in signals.observed}
    assert "name_keyed_channel" in types_emitted
    # All observed signals are scoped to the good bot.
    assert all(s["bot_id"] == "good" for s in signals.observed)
