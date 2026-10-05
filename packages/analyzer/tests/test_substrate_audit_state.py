"""Unit tests for substrate_audit_state — state assembly from local files."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))

from substrate_audit_state import (  # noqa: E402
    KNOWN_PROVIDERS,
    KNOWN_SKILLS,
    _days_since,
    _days_until,
    _parse_iso,
    assemble_provider_state,
    assemble_skill_state,
    gather_recent_skill_failures,
    read_auth_profiles,
    read_openclaw_json,
)


# ── Time helpers ─────────────────────────────────────────────────────────────


def test_parse_iso_handles_z_suffix() -> None:
    dt = _parse_iso("2026-05-17T10:00:00Z")
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_iso_returns_none_for_garbage() -> None:
    assert _parse_iso("not-a-date") is None
    assert _parse_iso(None) is None
    assert _parse_iso("") is None


def test_days_since_zero_for_now() -> None:
    now = datetime.now(timezone.utc)
    assert _days_since(now.isoformat(), now=now) == 0


def test_days_since_returns_positive() -> None:
    now = datetime.now(timezone.utc)
    past = (now - timedelta(days=5)).isoformat()
    assert _days_since(past, now=now) == 5


def test_days_until_negative_for_past() -> None:
    now = datetime.now(timezone.utc)
    past = (now - timedelta(days=3)).isoformat()
    assert _days_until(past, now=now) == -3


def test_days_until_positive_for_future() -> None:
    now = datetime.now(timezone.utc)
    future = (now + timedelta(days=10)).isoformat()
    assert _days_until(future, now=now) == 10


# ── Inventory constants ──────────────────────────────────────────────────────


def test_known_skills_includes_core_set() -> None:
    for required in ("gmail", "calendar", "slack", "telegram", "discord"):
        assert required in KNOWN_SKILLS


def test_known_providers_includes_google_workspace() -> None:
    assert "google_workspace" in KNOWN_PROVIDERS


# ── File readers (with tmp_path home) ────────────────────────────────────────


def test_read_openclaw_json_missing_returns_empty(tmp_path: Path) -> None:
    assert read_openclaw_json(home=tmp_path) == {}


def test_read_openclaw_json_parses_file(tmp_path: Path) -> None:
    oc_dir = tmp_path / ".openclaw"
    oc_dir.mkdir()
    (oc_dir / "openclaw.json").write_text(json.dumps({"plugins": {"entries": {}}}))
    assert read_openclaw_json(home=tmp_path) == {"plugins": {"entries": {}}}


def test_read_auth_profiles_missing_returns_empty(tmp_path: Path) -> None:
    assert read_auth_profiles(home=tmp_path) == {}


# ── assemble_skill_state ─────────────────────────────────────────────────────


def _setup_bot_home(
    tmp_path: Path, *, openclaw: dict | None = None, auth: dict | None = None,
) -> Path:
    oc_dir = tmp_path / ".openclaw"
    oc_dir.mkdir()
    if openclaw is not None:
        (oc_dir / "openclaw.json").write_text(json.dumps(openclaw))
    if auth is not None:
        (oc_dir / "auth-profiles.json").write_text(json.dumps(auth))
    return tmp_path


def test_assemble_skill_state_gmail_with_valid_token(tmp_path: Path) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    future_iso = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    home = _setup_bot_home(
        tmp_path,
        openclaw={"plugins": {"entries": {"google": {"enabled": True}}}},
        auth={"profiles": {"google_workspace:team_bot_a": {
            "status": "active",
            "scopes": ["gmail.readonly"],
            "issued_at": now_iso,
            "expires_at": future_iso,
        }}},
    )
    state = assemble_skill_state(skill_id="gmail", bot_id="team_bot_a", home=home)
    assert state.plugin_enabled is True
    assert state.oauth_profile_status == "active"
    assert state.scopes_present == ["gmail.readonly"]
    assert state.credentials_expire_days is not None
    assert state.credentials_expire_days >= 0
    assert state.has_oauth is True


def test_assemble_skill_state_gmail_missing_token(tmp_path: Path) -> None:
    home = _setup_bot_home(
        tmp_path,
        openclaw={"plugins": {"entries": {"google": {"enabled": True}}}},
        auth={"profiles": {}},
    )
    state = assemble_skill_state(skill_id="gmail", bot_id="team_bot_a", home=home)
    assert state.oauth_profile_status == "missing"


def test_assemble_skill_state_gmail_expired_token(tmp_path: Path) -> None:
    past_iso = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    home = _setup_bot_home(
        tmp_path,
        openclaw={"plugins": {"entries": {"google": {"enabled": True}}}},
        auth={"profiles": {"google_workspace:team_bot_a": {
            "status": "expired",
            "scopes": ["gmail.readonly"],
            "expires_at": past_iso,
        }}},
    )
    state = assemble_skill_state(skill_id="gmail", bot_id="team_bot_a", home=home)
    assert state.credentials_expire_days is not None
    assert state.credentials_expire_days < 0


def test_assemble_skill_state_gmail_scope_drift(tmp_path: Path) -> None:
    home = _setup_bot_home(
        tmp_path,
        openclaw={"plugins": {"entries": {"google": {"enabled": True}}}},
        auth={"profiles": {"google_workspace:team_bot_a": {
            "status": "active",
            "scopes": ["gmail.send"],   # send instead of readonly
        }}},
    )
    state = assemble_skill_state(skill_id="gmail", bot_id="team_bot_a", home=home)
    assert "gmail.readonly" in state.scopes_expected
    assert "gmail.send" in state.scopes_present
    # Scope drift surfaces in to_prompt_dict
    pd = state.to_prompt_dict()
    assert "gmail.readonly" in pd["scope_drift"]


def test_assemble_skill_state_credentialless_skill(tmp_path: Path) -> None:
    home = _setup_bot_home(
        tmp_path, openclaw={"plugins": {"entries": {}}}, auth={"profiles": {}},
    )
    state = assemble_skill_state(skill_id="imessage", bot_id="team_bot_a", home=home)
    assert state.has_oauth is False
    assert state.oauth_profile is None


def test_assemble_skill_state_collects_dependent_apps(tmp_path: Path) -> None:
    home = _setup_bot_home(
        tmp_path,
        openclaw={"plugins": {"entries": {"google": {"enabled": True}}}},
        auth={"profiles": {}},
    )
    manifests = tmp_path / ".openclaw" / "workspace" / "manifests"
    manifests.mkdir(parents=True)
    (manifests / "morning-briefing.json").write_text(json.dumps({
        "id": "morning-briefing",
        "requirements": {"integrations": [{"id": "gmail"}]},
    }))
    (manifests / "unrelated.json").write_text(json.dumps({
        "id": "unrelated",
        "requirements": {"integrations": [{"id": "slack"}]},
    }))
    state = assemble_skill_state(
        skill_id="gmail", bot_id="team_bot_a", home=home, manifests_dir=manifests,
    )
    assert "morning-briefing" in state.apps_using_skill
    assert "unrelated" not in state.apps_using_skill


# ── assemble_provider_state ──────────────────────────────────────────────────


def test_assemble_provider_state_present_active(tmp_path: Path) -> None:
    future_iso = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    home = _setup_bot_home(
        tmp_path,
        auth={"profiles": {"google_workspace:team_bot_a": {
            "status": "active",
            "scopes": ["gmail.readonly"],
            "expires_at": future_iso,
            "refresh_token": "secret",
        }}},
    )
    state = assemble_provider_state(provider_id="google_workspace", bot_id="team_bot_a", home=home)
    assert state.profile_present is True
    assert state.profile_status == "active"
    assert state.refresh_token_present is True
    assert state.token_expire_days is not None
    assert state.token_expire_days >= 0


def test_assemble_provider_state_missing_profile(tmp_path: Path) -> None:
    home = _setup_bot_home(tmp_path, auth={"profiles": {}})
    state = assemble_provider_state(provider_id="google_workspace", bot_id="team_bot_a", home=home)
    assert state.profile_present is False
    assert state.profile_status == "missing"


def test_assemble_provider_state_scopes_needed_by_dependent_skills(tmp_path: Path) -> None:
    home = _setup_bot_home(tmp_path, auth={"profiles": {}})
    state = assemble_provider_state(provider_id="google_workspace", bot_id="team_bot_a", home=home)
    # gmail + calendar map to google_workspace
    assert "gmail" in state.dependent_skills
    assert "calendar" in state.dependent_skills


# ── gather_recent_skill_failures ─────────────────────────────────────────────


def test_gather_recent_skill_failures_no_signal_store(tmp_path: Path) -> None:
    assert gather_recent_skill_failures(
        skill_id="gmail", bot_id="team_bot_a", shared_dir=tmp_path,
    ) == []


def test_gather_recent_skill_failures_matches_skill_id(tmp_path: Path) -> None:
    firing = tmp_path / "signals" / "firing"
    firing.mkdir(parents=True)
    sig_path = firing / "s1.json"
    # Phase B: gather_recent_skill_failures reads via signals.store.iter_active,
    # which only yields records that deserialize — include the required
    # Signal fields (signature/producer/flavor/scope), and last_observed_at
    # so the record falls inside the recency window.
    now_iso = datetime.now(timezone.utc).isoformat()
    sig_path.write_text(json.dumps({
        "id": "sig-1",
        "type": "skill_invocation_failed",
        "signature": "skill_audit:skill_invocation_failed:team_bot_a:gmail",
        "producer": "skill_audit",
        "flavor": "maintenance",
        "scope": "bot",
        "bot_id": "team_bot_a",
        "severity": "warn",
        "title": "Gmail failed",
        "details": {"skill_id": "gmail"},
        "first_seen": now_iso,
        "last_observed_at": now_iso,
    }))
    out = gather_recent_skill_failures(
        skill_id="gmail", bot_id="team_bot_a", shared_dir=tmp_path,
    )
    assert len(out) == 1
    assert out[0]["signal_id"] == "sig-1"


def test_gather_recent_skill_failures_filters_by_bot(tmp_path: Path) -> None:
    firing = tmp_path / "signals" / "firing"
    firing.mkdir(parents=True)
    (firing / "s.json").write_text(json.dumps({
        "id": "sig-other",
        "bot_id": "admin_bot",   # Wrong bot
        "details": {"skill_id": "gmail"},
    }))
    out = gather_recent_skill_failures(
        skill_id="gmail", bot_id="team_bot_a", shared_dir=tmp_path,
    )
    assert out == []
