"""EVO-SEP-S5 follow-up: reserved-name guard on the CREATION / PROVISION paths.

PR #3083 made the *removal* paths (retire_bot / delete_bot / remove_evolve_plugin
+ their CLI commands) refuse the reserved ids ``{evolve, evo}``. It did NOT guard
the paths that *create* a bot or *delete a user during provisioning rollback*.
This module pins that follow-up:

  A) ``deploy.add_bot`` — the single registration funnel for the wizard, the
     ``add-bot`` CLI, and the UI's Add Bot flow — refuses a reserved bot_id (and
     a reserved ``--user`` alias) BEFORE writing network.json.
  B) ``provisioning.provision_bot`` refuses at validation (Stage 1) — BEFORE any
     macOS user is created (Stage 2) — so the pipeline never stands up a reserved
     account, and never pushes a reserved-account delete onto its rollback stack.
  C) ``provisioning._dscl_delete_user`` — the rollback delete sink that calls
     ``get_isolation().delete_user(user, remove_home=True)`` — refuses a reserved
     account as a last-line backstop, never reaching the seam.
  D) the ``add-bot`` / ``provision-bot`` CLI commands refuse (incl. ``--dry-run``),
     while a normal bot still registers / provisions cleanly (no false positives).

Spec: docs/spec-evo-account-separation-2026-05-25.md.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from click.testing import CliRunner  # noqa: E402

from evolve_admin import provisioning  # noqa: E402
from evolve_admin.cli import main  # noqa: E402
from evolve_admin.config import RESERVED_BOT_IDS  # noqa: E402
from evolve_admin.deploy import add_bot  # noqa: E402
from evolve_admin.provisioning import (  # noqa: E402
    STAGE_VALIDATE,
    provision_bot,
)

RESERVED = sorted(RESERVED_BOT_IDS)  # ["evo", "evolve"]


# ── helpers ──────────────────────────────────────────────────────────────────


def _write_net(tmp_path: Path, *, members=None, bots=None) -> Path:
    """A minimal network.json with an existing non-reserved member."""
    shared = tmp_path / "shared"
    shared.mkdir(exist_ok=True)
    members = ["ghost"] if members is None else members
    p = tmp_path / "network.json"
    p.write_text(json.dumps({
        "networkId": "test-pod",
        "primary": members[0] if members else "ghost",
        "members": list(members),
        "sharedDir": str(shared),
        "bots": bots if bots is not None else {m: {"role": "member", "port": 19000 + i}
                                               for i, m in enumerate(members)},
    }, indent=2))
    return p


def _members(net: Path) -> list:
    return json.loads(net.read_text()).get("members", [])


# ── A: deploy.add_bot (registration funnel) ──────────────────────────────────


@pytest.mark.parametrize("bot_id", RESERVED)
def test_add_bot_refuses_reserved_bot_id(tmp_path, bot_id):
    net = _write_net(tmp_path)
    with pytest.raises(ValueError) as ei:
        add_bot(bot_id, port=19030, role="primary", network_path=net)
    assert "EVO-SEP-S5" in str(ei.value)
    # network.json untouched — the reserved id was never registered.
    assert bot_id not in _members(net)
    assert _members(net) == ["ghost"]


@pytest.mark.parametrize("reserved_user", RESERVED)
def test_add_bot_refuses_reserved_user_alias(tmp_path, reserved_user):
    """An innocuous bot_id whose explicit ``--user`` is reserved is still
    refused — otherwise the alias slips past a name-only check."""
    net = _write_net(tmp_path)
    with pytest.raises(ValueError) as ei:
        add_bot("helper", port=19031, user=reserved_user, network_path=net)
    assert "EVO-SEP-S5" in str(ei.value)
    assert "helper" not in _members(net)


def test_add_bot_registers_normal_bot(tmp_path):
    """No false positive — a normal bot still lands in network.json."""
    net = _write_net(tmp_path)
    add_bot("darwin", port=19032, network_path=net)
    assert "darwin" in _members(net)
    assert json.loads(net.read_text())["bots"]["darwin"]["port"] == 19032


# ── B: provision_bot validation (before user creation) ───────────────────────


@pytest.mark.parametrize("bot_id", RESERVED)
def test_provision_bot_refuses_reserved_before_creating_user(tmp_path, bot_id):
    net = _write_net(tmp_path)
    # Tripwire every side-effecting sink past validation — if the guard failed
    # the test would surface it (create called, or members mutated).
    with patch.object(provisioning, "_create_macos_user") as m_create, \
         patch.object(provisioning, "_user_exists", return_value=False), \
         patch.object(provisioning, "_dscl_delete_user") as m_delete:
        result = provision_bot(bot_id, port=19033, role="primary", network_path=net)

    assert result.success is False
    assert result.failed_stage == STAGE_VALIDATE
    assert "EVO-SEP-S5" in (result.error or "")
    # No user created, no rollback delete, no roster mutation.
    m_create.assert_not_called()
    m_delete.assert_not_called()
    assert bot_id not in _members(net)


@pytest.mark.parametrize("reserved_user", RESERVED)
def test_provision_bot_refuses_reserved_user_alias(tmp_path, reserved_user):
    net = _write_net(tmp_path)
    with patch.object(provisioning, "_create_macos_user") as m_create, \
         patch.object(provisioning, "_user_exists", return_value=False):
        result = provision_bot(
            "helper", user=reserved_user, port=19034, network_path=net,
        )
    assert result.success is False
    assert result.failed_stage == STAGE_VALIDATE
    assert "EVO-SEP-S5" in (result.error or "")
    m_create.assert_not_called()


@pytest.mark.parametrize("bot_id", RESERVED)
def test_provision_bot_refuses_reserved_even_on_dry_run(tmp_path, bot_id):
    """``--dry-run`` runs validation first, so the guard fires there too —
    a dry-run must never report it *would* provision a reserved account."""
    net = _write_net(tmp_path)
    with patch.object(provisioning, "_create_macos_user") as m_create:
        result = provision_bot(bot_id, port=19035, network_path=net, dry_run=True)
    assert result.success is False
    assert result.failed_stage == STAGE_VALIDATE
    assert "EVO-SEP-S5" in (result.error or "")
    m_create.assert_not_called()


# ── C: _dscl_delete_user backstop (rollback delete sink) ─────────────────────


@pytest.mark.parametrize("user", RESERVED)
def test_dscl_delete_user_refuses_reserved_account(user):
    """The last-line backstop: even called directly, the rollback delete sink
    refuses a reserved account and never reaches ``get_isolation().delete_user``."""
    fake_iso = MagicMock()
    with patch.object(provisioning, "get_isolation", return_value=fake_iso):
        with pytest.raises(provisioning.ProvisionError) as ei:
            provisioning._dscl_delete_user(user, remove_home=True)
    assert "EVO-SEP-S5" in str(ei.value)
    fake_iso.delete_user.assert_not_called()


def test_dscl_delete_user_allows_normal_account():
    """A normal account still deletes — the backstop only blocks reserved ids."""
    fake_iso = MagicMock()
    with patch.object(provisioning, "get_isolation", return_value=fake_iso):
        provisioning._dscl_delete_user("darwin", remove_home=True)
    fake_iso.delete_user.assert_called_once_with("darwin", remove_home=True)


# ── D: CLI layer (add-bot / provision-bot) ───────────────────────────────────


@pytest.mark.parametrize("bot_id", RESERVED)
def test_cli_add_bot_refuses_reserved_dry_run(tmp_path, bot_id):
    """``add-bot <reserved> --dry-run`` short-circuits with a clean refusal —
    before the 'Would register …' planning line."""
    net = _write_net(tmp_path)
    r = CliRunner().invoke(
        main, ["--network", str(net), "add-bot", bot_id, "--port", "19036", "--dry-run"],
    )
    assert r.exit_code == 1, r.output
    assert "EVO-SEP-S5" in r.output
    assert "Would register" not in r.output
    assert bot_id not in _members(net)


def test_cli_add_bot_allows_normal_dry_run(tmp_path):
    net = _write_net(tmp_path)
    r = CliRunner().invoke(
        main, ["--network", str(net), "add-bot", "darwin", "--port", "19037", "--dry-run"],
    )
    assert r.exit_code == 0, r.output
    assert "Would register darwin" in r.output
    # Dry-run mutates nothing.
    assert "darwin" not in _members(net)


@pytest.mark.parametrize("bot_id", RESERVED)
def test_cli_provision_bot_refuses_reserved(tmp_path, bot_id):
    """``provision-bot <reserved>`` fails at the validate stage. ``--no-onboard``
    skips the auth-choice precondition so the reserved guard is the only stop."""
    net = _write_net(tmp_path)
    with patch.object(provisioning, "_create_macos_user") as m_create:
        r = CliRunner().invoke(
            main,
            ["--network", str(net), "provision-bot", bot_id,
             "--port", "19038", "--no-onboard", "--no-deploy"],
        )
    assert r.exit_code == 1, r.output
    assert "EVO-SEP-S5" in r.output
    m_create.assert_not_called()
    assert bot_id not in _members(net)
