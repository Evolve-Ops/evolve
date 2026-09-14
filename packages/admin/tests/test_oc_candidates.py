"""Tests for the advisory-only OpenClaw discovery scan.

`find_oc_candidates()` replaced the broken `discover_bots()` which had a
dedupe-by-port path that miscalculated user-vs-bot_id and ended up
auto-registering phantom bots (incident 2026-05-03: personal_bot_user + forge).

The new function:
  - returns OcCandidate tuples keyed by (user, config_path), no bot_id
    field — registration under a bot_id is the operator's explicit decision
  - never writes to network.json
  - flags candidates as `is_pod_member` when the network already lists them
  - has no hardcoded user/bot/account name lists
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import wizard  # noqa: E402
from evolve_admin.wizard import (  # noqa: E402
    OcCandidate,
    _looks_like_admin_account,
    find_oc_candidates,
)


# ── Fixture helpers ───────────────────────────────────────────────────────────


def _make_users_root(tmp_path: Path, layout: dict[str, dict | None]) -> Path:
    """Build a fake /Users/ tree.

    layout = {username: {"port": int, "extra_config": dict} | None}
    None means create the user dir but no .openclaw — should not appear as
    a candidate. Anything else creates .openclaw/openclaw.json with the
    given gateway port.
    """
    users = tmp_path / "Users"
    users.mkdir()
    for uname, spec in layout.items():
        d = users / uname
        d.mkdir()
        if spec is None:
            continue
        oc_dir = d / ".openclaw"
        oc_dir.mkdir()
        cfg = {"gateway": {"port": spec["port"]}}
        cfg.update(spec.get("extra_config", {}))
        (oc_dir / "openclaw.json").write_text(json.dumps(cfg))
    return users


@pytest.fixture
def fake_users(tmp_path: Path, monkeypatch):
    """Patch the /Users/ scan + helpers to a sandboxed tree."""
    users_root = _make_users_root(tmp_path, {
        "team_bot_a":     {"port": 18789},          # plain bot
        "personal_bot_user": {"port": 18790},          # team_bot_b lives here (different bot_id)
        "forge":   {"port": 19030},          # historical leftover, not in pod
        "Shared":  None,                      # macOS system dir, no .openclaw
        ".trash":  {"port": 0},                # dotfile dir, must be ignored
    })

    # Patch the path-walk root used by find_oc_candidates
    real_path_init = wizard.Path
    monkeypatch.setattr(
        wizard,
        "Path",
        lambda *a, **kw: (
            users_root if (a == ("/Users",)) else real_path_init(*a, **kw)
        ),
    )

    # No real launchd, no real dscl
    monkeypatch.setattr(wizard, "_gateway_plists_by_user", lambda: {})
    monkeypatch.setattr(wizard, "_looks_like_admin_account", lambda u: False)
    return users_root


# ── find_oc_candidates ────────────────────────────────────────────────────────


def test_finds_all_oc_installs(fake_users):
    candidates = find_oc_candidates()
    users = sorted(c.user for c in candidates)
    # forge, personal_bot_user, team_bot_a each have .openclaw/openclaw.json — all are candidates.
    # Shared and .trash do NOT (Shared has no .openclaw; .trash is a dotfile).
    assert users == ["forge", "personal_bot_user", "team_bot_a"]


def test_dotfile_dirs_are_skipped(fake_users):
    """`.trash` has an openclaw.json but the leading dot disqualifies it."""
    candidates = find_oc_candidates()
    assert all(not c.user.startswith(".") for c in candidates)


def test_reads_gateway_port_from_config(fake_users):
    candidates = {c.user: c for c in find_oc_candidates()}
    assert candidates["team_bot_a"].port == 18789
    assert candidates["personal_bot_user"].port == 18790
    assert candidates["forge"].port == 19030


def test_does_not_mutate_network_json(fake_users, tmp_path: Path):
    """The single most important contract: discovery never writes."""
    net_path = tmp_path / "network.json"
    net_path.write_text(json.dumps({"bots": {}, "members": []}))
    pre = net_path.read_text()
    pre_mtime = net_path.stat().st_mtime

    find_oc_candidates(network=json.loads(pre))

    assert net_path.read_text() == pre
    assert net_path.stat().st_mtime == pre_mtime


def test_marks_pod_member_by_user(fake_users):
    """A registered bot's macOS user marks the candidate as already-in-pod."""
    network = {
        "bots": {
            "team_bot_a": {"role": "member", "port": 18789},
            "team_bot_b": {"role": "member", "port": 18790, "user": "personal_bot_user"},
        },
    }
    candidates = {c.user: c for c in find_oc_candidates(network=network)}
    assert candidates["team_bot_a"].is_pod_member is True
    assert candidates["personal_bot_user"].is_pod_member is True
    assert candidates["forge"].is_pod_member is False  # not in pod


def test_marks_pod_member_by_port(fake_users):
    """If a candidate's port matches a pod bot's port, it's the same install."""
    network = {"bots": {"someid": {"role": "member", "port": 18789}}}
    candidates = {c.user: c for c in find_oc_candidates(network=network)}
    # team_bot_a's port 18789 matches the registered bot
    assert candidates["team_bot_a"].is_pod_member is True


def test_no_bot_id_field_on_candidates(fake_users):
    """OcCandidate identifies installs by (user, config_path) — bot_id is the
    operator's choice at registration time. Mistaking a discovered user for
    a bot_id was the root cause of the 2026-05-03 phantom-bot incident."""
    candidates = find_oc_candidates()
    for c in candidates:
        assert not hasattr(c, "bot_id")
        assert isinstance(c, OcCandidate)


def test_no_network_argument_works(fake_users):
    """Without a network, all candidates report is_pod_member=False."""
    candidates = find_oc_candidates()
    assert all(c.is_pod_member is False for c in candidates)


# ── plist matching ────────────────────────────────────────────────────────────


def test_suggested_bot_id_parsed_from_matched_plist(tmp_path, monkeypatch):
    users_root = _make_users_root(tmp_path, {"personal_bot_user": {"port": 18790}})
    real_path_init = wizard.Path
    monkeypatch.setattr(
        wizard, "Path",
        lambda *a, **kw: (
            users_root if (a == ("/Users",)) else real_path_init(*a, **kw)
        ),
    )
    # Plist's UserName is "personal_bot_user", label is "ai.openclaw.team_bot_b-gateway"
    monkeypatch.setattr(
        wizard, "_gateway_plists_by_user",
        lambda: {"personal_bot_user": "ai.openclaw.team_bot_b-gateway"},
    )
    monkeypatch.setattr(wizard, "_looks_like_admin_account", lambda u: False)

    [c] = find_oc_candidates()
    assert c.user == "personal_bot_user"
    assert c.suggested_bot_id == "team_bot_b"
    assert c.plist_label == "ai.openclaw.team_bot_b-gateway"


def test_no_plist_means_no_suggested_bot_id(fake_users):
    candidates = {c.user: c for c in find_oc_candidates()}
    # fake_users patches _gateway_plists_by_user to return {}
    assert all(c.suggested_bot_id is None for c in candidates.values())
    assert all(c.plist_label is None for c in candidates.values())


# ── _looks_like_admin_account ─────────────────────────────────────────────────


def test_looks_like_admin_matches_sudo_user(monkeypatch):
    monkeypatch.setenv("SUDO_USER", "pod_admin_user")
    # Avoid hitting real dscl
    with patch("evolve_admin.wizard.subprocess.run") as mock_run:
        mock_run.return_value.stdout = ""
        assert _looks_like_admin_account("pod_admin_user") is True
        assert _looks_like_admin_account("team_bot_a") is False


def test_looks_like_admin_reads_admin_group(monkeypatch):
    monkeypatch.delenv("SUDO_USER", raising=False)
    with patch("evolve_admin.wizard.subprocess.run") as mock_run:
        mock_run.return_value.stdout = "GroupMembership: pod_admin_user root admin\n"
        mock_run.return_value.returncode = 0
        assert _looks_like_admin_account("pod_admin_user") is True
        assert _looks_like_admin_account("team_bot_a") is False


def test_looks_like_admin_no_hardcoded_names(monkeypatch):
    """Heuristic must be data-driven — no static user/bot lists."""
    monkeypatch.delenv("SUDO_USER", raising=False)
    with patch("evolve_admin.wizard.subprocess.run") as mock_run:
        mock_run.return_value.stdout = ""
        # Names that used to be in the historical SKIP_USERS list must not
        # be flagged as admin without evidence from sudo/dscl.
        for name in ("forge", "evolve", "sandbox", "Shared", "Guest", "personal_bot_user"):
            assert _looks_like_admin_account(name) is False
