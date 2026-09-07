"""Tests for the §5.2 arming toggle — ``evolve-admin breaker arm|disarm``.

The arming PR flips ``breakers.auto_trip_enabled`` to default-armed in
``breakers.runner`` and adds the operator toggle here, so arming is a
config decision, not a code change. Covered:

  • ``breaker disarm`` records ``auto_trip_enabled: false`` and the
    runner's flag reader honors it (observe-only).
  • ``breaker arm`` records ``auto_trip_enabled: true`` and the reader
    honors it — round-trip through the same network.json the runner
    reads, proving the toggle is honored in both positions.
  • ``breaker status`` surfaces the arming state (human + ``--json``).
  • The group survived its move out of cli.py (registration smoke).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (str(_ADMIN), str(_ANALYZER)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def cli_pod(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from evolve_admin import breakers_cli as _bcli

    shared = tmp_path / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    network = {
        "networkId": "test-pod",
        "sharedDir": str(shared),
        "members": ["team_bot_a"],
        "bots": {"team_bot_a": {"role": "member", "port": 19002, "user": "team_bot_a"}},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    # breakers_cli calls save_network (staged-copy write, sudo fallback);
    # in tests a plain write to the tmp path is the same contract.
    def _save(data, path=None):
        Path(path or network_path).write_text(json.dumps(data, indent=2))

    monkeypatch.setattr(_bcli, "save_network", _save)
    return CliRunner(), network_path


def _run(runner, network_path, *args):
    from evolve_admin.cli import main
    return runner.invoke(main, ["--network", str(network_path), *args])


def test_disarm_records_false_and_runner_honors_it(cli_pod):
    from breakers.runner import read_auto_trip_enabled

    runner, network_path = cli_pod
    r = _run(runner, network_path, "breaker", "disarm")
    assert r.exit_code == 0, r.output
    assert "disarmed" in r.output

    network = json.loads(network_path.read_text())
    assert network["breakers"]["auto_trip_enabled"] is False
    assert read_auto_trip_enabled(network) is False


def test_arm_records_true_and_runner_honors_it(cli_pod):
    from breakers.runner import read_auto_trip_enabled

    runner, network_path = cli_pod
    # Round-trip: disarm first, then arm — arm must clear the opt-out.
    assert _run(runner, network_path, "breaker", "disarm").exit_code == 0
    r = _run(runner, network_path, "breaker", "arm")
    assert r.exit_code == 0, r.output
    assert "armed" in r.output

    network = json.loads(network_path.read_text())
    assert network["breakers"]["auto_trip_enabled"] is True
    assert read_auto_trip_enabled(network) is True


def test_toggle_preserves_sibling_breakers_keys(cli_pod):
    """The toggle must load-modify-save, not clobber other breakers.* keys
    (e.g. runner_log_full_verbosity from a calibration soak)."""
    runner, network_path = cli_pod
    network = json.loads(network_path.read_text())
    network["breakers"] = {"runner_log_full_verbosity": True}
    network_path.write_text(json.dumps(network))

    assert _run(runner, network_path, "breaker", "disarm").exit_code == 0
    network = json.loads(network_path.read_text())
    assert network["breakers"]["auto_trip_enabled"] is False
    assert network["breakers"]["runner_log_full_verbosity"] is True


def test_status_shows_armed_by_default(cli_pod):
    runner, network_path = cli_pod
    r = _run(runner, network_path, "breaker", "status", "--audit-days", "0")
    assert r.exit_code == 0, r.output
    assert "ARMED" in r.output
    assert "code default" in r.output


def test_status_shows_disarmed_after_disarm(cli_pod):
    runner, network_path = cli_pod
    assert _run(runner, network_path, "breaker", "disarm").exit_code == 0
    r = _run(runner, network_path, "breaker", "status", "--audit-days", "0")
    assert r.exit_code == 0, r.output
    assert "DISARMED" in r.output
    assert "explicit in network.json" in r.output


def test_status_json_carries_arming_state(cli_pod):
    runner, network_path = cli_pod
    r = _run(runner, network_path, "breaker", "status", "--audit-days", "0", "--json")
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["auto_trip_enabled"] is True
    assert payload["trips"] == []

    assert _run(runner, network_path, "breaker", "disarm").exit_code == 0
    r = _run(runner, network_path, "breaker", "status", "--audit-days", "0", "--json")
    payload = json.loads(r.output)
    assert payload["auto_trip_enabled"] is False


def test_breaker_group_registered_on_main(cli_pod):
    """The move out of cli.py must keep every subcommand reachable."""
    runner, network_path = cli_pod
    r = _run(runner, network_path, "breaker", "--help")
    assert r.exit_code == 0, r.output
    for sub in ("trip", "reset", "extend", "status", "arm", "disarm"):
        assert sub in r.output
