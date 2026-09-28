"""Tests for ``evolve-admin intake configure`` multi-target writes.

Contract:
  - First-run with no --name flag writes the v1 single-target schema
    (owner/repo at top of intake.github).
  - Second run with --name <X> upgrades the existing config into v2
    (targets dict + default) AND folds the v1 entry in as 'default'.
  - --make-default flips the default-target field.
  - Reader (PromotionConfig.from_network) accepts both shapes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

from evolve_admin import cli as cli_mod  # noqa: E402
from evolve_admin.cli import main  # noqa: E402
from evolve_admin.intake import promote as _promote  # noqa: E402


@pytest.fixture()
def isolated_pod(tmp_path: Path, monkeypatch):
    """Point cli at a tmp network.json + shared_dir; stub the keystore so
    we don't write secrets to disk."""
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({"sharedDir": str(tmp_path)}))

    # The CLI's pass_context expects a network_path in ctx.obj. Easiest
    # path: invoke with --network so the cli's group sets ctx.obj for us.
    # We also need to stub KeystoreManager since it would create a keys.json
    # on every configure call.
    class _FakeMgr:
        def __init__(self, *a, **kw):
            self.ks = _FakeKeystore()
        def register(self, *a, **kw): pass
        def set_value(self, *a, **kw): pass

    class _FakeKeystore:
        def get_key_entry(self, slot): return None

    monkeypatch.setattr("evolve_admin.keystore.KeystoreManager", _FakeMgr)
    return network_path


def _read_network(network_path: Path) -> dict:
    return json.loads(network_path.read_text())


def test_first_run_no_name_writes_v1_single_target(isolated_pod: Path):
    """No --name on a fresh install → legacy single-target shape preserved."""
    runner = CliRunner()
    result = runner.invoke(main, [
        "--network", str(isolated_pod), "intake", "configure",
        "--owner", "evolve-ops", "--repo", "evolve",
    ])
    assert result.exit_code == 0, result.output

    network = _read_network(isolated_pod)
    block = network["intake"]["github"]
    # v1 shape: owner/repo at the top, no 'targets' nesting.
    assert block["owner"] == "evolve-ops"
    assert block["repo"] == "evolve"
    assert "targets" not in block
    # Reader handles it as a single "default" target.
    cfg = _promote.PromotionConfig.from_network(network)
    assert cfg is not None
    assert cfg.target_names == ["default"]


def test_second_run_with_name_upgrades_to_v2(isolated_pod: Path):
    """Adding a named target on top of an existing v1 install migrates
    forward AND preserves the original as 'default'."""
    runner = CliRunner()
    r1 = runner.invoke(main, [
        "--network", str(isolated_pod), "intake", "configure",
        "--owner", "evolve-ops", "--repo", "evolve",
    ])
    assert r1.exit_code == 0, r1.output

    r2 = runner.invoke(main, [
        "--network", str(isolated_pod), "intake", "configure",
        "--name", "openclaw",
        "--owner", "openclaw", "--repo", "openclaw",
        "--token-slot", "github_intake_openclaw",
    ])
    assert r2.exit_code == 0, r2.output

    network = _read_network(isolated_pod)
    block = network["intake"]["github"]
    assert "targets" in block
    assert set(block["targets"].keys()) == {"default", "openclaw"}
    # The original v1 target is now the v2 'default' entry, untouched.
    assert block["targets"]["default"]["owner"] == "evolve-ops"
    assert block["targets"]["default"]["repo"] == "evolve"
    # And the new target is in there.
    assert block["targets"]["openclaw"]["owner"] == "openclaw"
    assert block["targets"]["openclaw"]["token_slot"] == "github_intake_openclaw"
    # Default-name unchanged by a non-default-flagged add.
    assert block["default"] == "default"


def test_make_default_flips_default_target(isolated_pod: Path):
    """--make-default during configure should change which target is the
    fallback for un-suffixed promotes."""
    runner = CliRunner()
    runner.invoke(main, [
        "--network", str(isolated_pod), "intake", "configure",
        "--owner", "evolve-ops", "--repo", "evolve",
    ])
    runner.invoke(main, [
        "--network", str(isolated_pod), "intake", "configure",
        "--name", "openclaw",
        "--owner", "openclaw", "--repo", "openclaw",
    ])
    # Now flip default → openclaw.
    r = runner.invoke(main, [
        "--network", str(isolated_pod), "intake", "configure",
        "--name", "openclaw",
        "--owner", "openclaw", "--repo", "openclaw",
        "--make-default",
    ])
    assert r.exit_code == 0, r.output

    network = _read_network(isolated_pod)
    assert network["intake"]["github"]["default"] == "openclaw"


def test_first_run_with_name_writes_v2_directly(isolated_pod: Path):
    """When the operator passes --name on first run, skip the v1 form
    entirely and write v2."""
    runner = CliRunner()
    r = runner.invoke(main, [
        "--network", str(isolated_pod), "intake", "configure",
        "--name", "evolve",
        "--owner", "evolve-ops", "--repo", "evolve",
    ])
    assert r.exit_code == 0, r.output

    block = _read_network(isolated_pod)["intake"]["github"]
    assert block["default"] == "evolve"
    assert "owner" not in block  # no v1 top-level leak
    assert block["targets"]["evolve"]["owner"] == "evolve-ops"


def test_list_targets_reports_configured_targets(isolated_pod: Path):
    """`intake list-targets` should print every configured target with
    a clear indicator of which is default."""
    runner = CliRunner()
    runner.invoke(main, [
        "--network", str(isolated_pod), "intake", "configure",
        "--name", "evolve",
        "--owner", "evolve-ops", "--repo", "evolve",
    ])
    runner.invoke(main, [
        "--network", str(isolated_pod), "intake", "configure",
        "--name", "openclaw",
        "--owner", "openclaw", "--repo", "openclaw",
    ])

    r = runner.invoke(main, [
        "--network", str(isolated_pod), "intake", "list-targets",
    ])
    assert r.exit_code == 0, r.output
    out = r.output
    assert "evolve" in out
    assert "openclaw" in out
    assert "evolve-ops/evolve" in out
    assert "openclaw/openclaw" in out


def test_list_targets_empty_pod_explains(isolated_pod: Path):
    """On a fresh pod with no targets, list-targets should give a clear
    hint about how to configure one (not a stack trace)."""
    runner = CliRunner()
    r = runner.invoke(main, [
        "--network", str(isolated_pod), "intake", "list-targets",
    ])
    assert r.exit_code == 0, r.output
    assert "no intake targets configured" in r.output
    assert "intake configure" in r.output
