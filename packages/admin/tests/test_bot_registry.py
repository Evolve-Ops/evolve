"""Tests for explicit pod-membership invariants.

Pod membership lives in network.json's `bots` dict and `members` array.
The single explicit-add path is `add_bot()`. `deploy_bot()` refuses
unknown bot_ids — this blocks the historical backdoors (discover_bots
auto-write, `evolve-admin deploy <newbot> --port X` auto-create) that
let phantom bots accumulate (incident 2026-05-03: personal_bot_user + forge).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.deploy import (  # noqa: E402
    DEFAULT_NEW_BOT_DAILY_CAP_USD,
    add_bot,
    deploy_bot,
)


def _write_network(path: Path, network: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Point sharedDir at tmp so add_bot's better-engine-config write (the
    # created_at stamp + any explicit cap) stays hermetic and never escapes to
    # the canonical /Users/Shared/evolve store.
    network = {"sharedDir": str(path.parent), **network}
    path.write_text(json.dumps(network, indent=2))


def _read_network(path: Path) -> dict:
    return json.loads(path.read_text())


# ── add_bot ───────────────────────────────────────────────────────────────────


def test_add_bot_writes_minimal_entry(tmp_path: Path):
    """Default add_bot leaves the safety-belt cap on — operators can
    clear it later but the default is "cap present" so a misconfigured
    new bot can't burn unlimited overnight (2026-06-03 personal-bot
    reference incident)."""
    net_path = tmp_path / "network.json"
    _write_network(net_path, {"bots": {}, "members": []})

    add_bot("admin_bot", port=19000, network_path=net_path)

    network = _read_network(net_path)
    assert network["bots"]["admin_bot"] == {
        "role": "member",
        "port": 19000,
        "multiUser": False,
        "daily_cap_usd": DEFAULT_NEW_BOT_DAILY_CAP_USD,
    }
    assert network["members"] == ["admin_bot"]


def test_add_bot_explicit_cap_overrides_default(tmp_path: Path):
    """Operator-provided cap wins over the default — e.g. a high-spend
    primary bot may need a larger envelope on day 1."""
    net_path = tmp_path / "network.json"
    _write_network(net_path, {"bots": {}, "members": []})

    add_bot(
        "evolve", port=19030, role="primary",
        daily_cap_usd=25.0, network_path=net_path,
    )
    assert _read_network(net_path)["bots"]["evolve"]["daily_cap_usd"] == 25.0


def test_add_bot_none_cap_skips_field(tmp_path: Path):
    """Passing ``daily_cap_usd=None`` opts the new bot out of the cap
    (operator's explicit decision). Field MUST be absent — that's how
    action_cost / setup_checklist read "no cap"."""
    net_path = tmp_path / "network.json"
    _write_network(net_path, {"bots": {}, "members": []})

    add_bot("admin_bot", port=19000, daily_cap_usd=None, network_path=net_path)

    assert "daily_cap_usd" not in _read_network(net_path)["bots"]["admin_bot"]


def test_add_bot_zero_or_negative_cap_skips_field(tmp_path: Path):
    """Mirrors action_cost: ≤ 0 is "no cap", same as absent. Avoids the
    trap where a 0 cap silently trips the breaker forever."""
    net_path = tmp_path / "network.json"
    _write_network(net_path, {"bots": {}, "members": []})

    add_bot("admin_bot", port=19000, daily_cap_usd=0, network_path=net_path)
    assert "daily_cap_usd" not in _read_network(net_path)["bots"]["admin_bot"]

    # Clean up and try negative.
    net_path.unlink()
    _write_network(net_path, {"bots": {}, "members": []})
    add_bot("admin_bot", port=19000, daily_cap_usd=-1.5, network_path=net_path)
    assert "daily_cap_usd" not in _read_network(net_path)["bots"]["admin_bot"]


def test_add_bot_records_user_when_different(tmp_path: Path):
    net_path = tmp_path / "network.json"
    _write_network(net_path, {"bots": {}, "members": []})

    add_bot("team_bot_b", port=18790, user="personal_bot_user", network_path=net_path)

    network = _read_network(net_path)
    assert network["bots"]["team_bot_b"]["user"] == "personal_bot_user"


def test_add_bot_omits_user_when_same_as_bot_id(tmp_path: Path):
    net_path = tmp_path / "network.json"
    _write_network(net_path, {"bots": {}, "members": []})

    add_bot("admin_bot", port=19000, user="admin_bot", network_path=net_path)

    network = _read_network(net_path)
    assert "user" not in network["bots"]["admin_bot"]


def test_add_bot_sets_primary_pointer_for_primary_role(tmp_path: Path):
    net_path = tmp_path / "network.json"
    _write_network(net_path, {"bots": {}, "members": []})

    add_bot("darwin", port=19030, role="primary", network_path=net_path)

    network = _read_network(net_path)
    assert network["primary"] == "darwin"
    assert network["bots"]["darwin"]["role"] == "primary"


def test_add_bot_multi_user_and_backup_url(tmp_path: Path):
    net_path = tmp_path / "network.json"
    _write_network(net_path, {"bots": {}, "members": []})

    add_bot(
        "team_bot_c", port=19020,
        multi_user=True,
        backup_repo_url="git@github.com:org/team_bot_c-workspace.git",
        network_path=net_path,
    )

    entry = _read_network(net_path)["bots"]["team_bot_c"]
    assert entry["multiUser"] is True
    assert entry["backupRepoUrl"] == "git@github.com:org/team_bot_c-workspace.git"


def test_add_bot_raises_when_already_registered(tmp_path: Path):
    net_path = tmp_path / "network.json"
    _write_network(net_path, {"bots": {"team_bot_a": {"role": "member", "port": 18789}},
                              "members": ["team_bot_a"]})

    with pytest.raises(ValueError, match="already registered"):
        add_bot("team_bot_a", port=18789, network_path=net_path)


def test_add_bot_idempotent_members_dedupe(tmp_path: Path):
    """If bot_id already in members but not bots (corrupt state), add_bot
    raises on the bots check — it does NOT silently re-add. Forces operator
    to detach-bot (or retire-bot / delete-bot) first, which is the
    explicit recovery path."""
    net_path = tmp_path / "network.json"
    _write_network(net_path, {"bots": {"team_bot_a": {"port": 18789}}, "members": ["team_bot_a"]})

    with pytest.raises(ValueError):
        add_bot("team_bot_a", port=18789, network_path=net_path)


# ── deploy_bot precondition ───────────────────────────────────────────────────


def test_deploy_bot_refuses_unknown_bot_id(tmp_path: Path):
    """The historical phantom-bot backdoor: deploy_bot used to auto-add
    any bot_id to network.json. After the lockdown, unknown ids return
    a failed DeployResult before doing any work."""
    net_path = tmp_path / "network.json"
    _write_network(net_path, {"bots": {"team_bot_a": {"role": "member", "port": 18789}},
                              "members": ["team_bot_a"]})

    result = deploy_bot("personal_bot_user", "member", 18790, net_path)

    assert result.success is False
    assert any("not a registered pod member" in e for e in result.errors)
    # network.json must NOT have been mutated
    network = _read_network(net_path)
    assert "personal_bot_user" not in network["bots"]
    assert "personal_bot_user" not in network["members"]


def test_deploy_bot_refuses_unknown_bot_id_short_circuits(tmp_path: Path):
    """The refusal must happen before workspace lookup, sudo calls, etc.
    — the test passing without sudo-auth or filesystem setup proves it
    doesn't reach the heavy work path."""
    net_path = tmp_path / "network.json"
    _write_network(net_path, {"bots": {}, "members": []})

    # Would explode if it reached _sudo_chown / ensure_pod_perms / etc.
    result = deploy_bot("phantom", "member", 99999, net_path)

    assert result.success is False
    assert "phantom" in result.errors[0]


def test_deploy_bot_error_message_points_to_add_bot(tmp_path: Path):
    """Operators recovering from a phantom-bot incident need a clear path
    forward. The error tells them exactly which command to run."""
    net_path = tmp_path / "network.json"
    _write_network(net_path, {"bots": {}, "members": []})

    result = deploy_bot("forge", "member", 19030, net_path)

    msg = " ".join(result.errors)
    assert "evolve-admin add-bot forge" in msg or "Add Bot" in msg
