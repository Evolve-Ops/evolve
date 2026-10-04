"""`evolve-admin auth` CLI — enable / disable / status (roadmap 2.6)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.delenv("EVOLVE_ADMIN_AUTH_DISABLED", raising=False)
    from evolve_admin.cli import main
    from evolve_admin.web import admin_auth

    shared = tmp_path / "shared"
    shared.mkdir()
    net = tmp_path / "network.json"
    net.write_text(json.dumps({"sharedDir": str(shared)}))
    return main, admin_auth, shared, str(net)


def test_status_enabled_by_default(cli_env):
    main, _aa, _shared, net = cli_env
    r = CliRunner().invoke(main, ["--network", net, "auth", "status"])
    assert r.exit_code == 0
    assert "ENABLED" in r.output


def test_disable_records_marker_then_enable_clears(cli_env):
    main, admin_auth, shared, net = cli_env
    runner = CliRunner()

    r = runner.invoke(main, ["--network", net, "auth", "disable",
                             "--accept-risk", "dedicated dev box"])
    assert r.exit_code == 0
    assert admin_auth.is_optout(shared) is True
    marker = json.loads(admin_auth._optout_path(shared).read_text())
    assert marker["reason"] == "dedicated dev box"
    assert marker["disabled"] is True

    r = runner.invoke(main, ["--network", net, "auth", "status"])
    assert "DISABLED" in r.output

    r = runner.invoke(main, ["--network", net, "auth", "enable"])
    assert r.exit_code == 0
    assert admin_auth.is_optout(shared) is False


def test_disable_requires_accept_risk(cli_env):
    main, _aa, _shared, net = cli_env
    r = CliRunner().invoke(main, ["--network", net, "auth", "disable"])
    assert r.exit_code != 0  # --accept-risk is required


def test_pair_refuses_unprivileged_before_minting_key(cli_env, monkeypatch):
    """The lockout guard: running `pair` as a non-root user (who can't chown
    the key to the evolve daemon) must refuse BEFORE creating a key the daemon
    can't read."""
    main, admin_auth, shared, net = cli_env
    # Simulate a non-root user. The key is absent (fresh tmp), so os.access
    # returns False naturally → the guard fires before minting.
    monkeypatch.setattr("os.geteuid", lambda: 501)
    r = CliRunner().invoke(main, ["--network", net, "pair"])
    assert r.exit_code == 1
    assert "sudo evolve-admin pair" in r.output
    # No key was minted — the lockout is avoided, not created.
    assert not admin_auth._key_path(shared).exists()
