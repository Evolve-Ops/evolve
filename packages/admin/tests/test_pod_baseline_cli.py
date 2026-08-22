"""``evolve-admin pod-baseline`` CLI: register pattern + census/seed flows."""
import json
import os

import click
import pytest
from click.testing import CliRunner

from evolve_admin.pod_baseline_cli import register_cli


def _make_cli(network_path):
    @click.group()
    @click.pass_context
    def main(ctx):
        ctx.ensure_object(dict)
        ctx.obj["network_path"] = network_path

    register_cli(main)
    return main


@pytest.fixture()
def pod(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({"bots": {}, "sharedDir": str(shared)}))
    return _make_cli(network_path), shared


def test_census_without_baseline_points_at_seed(pod):
    cli, shared = pod
    result = CliRunner().invoke(cli, ["pod-baseline", "census"])
    assert result.exit_code != 0
    assert "pod-baseline seed" in result.output


def test_seed_writes_baseline_and_prints_choices(pod):
    cli, shared = pod
    result = CliRunner().invoke(cli, ["pod-baseline", "seed"])
    assert result.exit_code == 0, result.output
    baseline_file = shared / "pod-baseline.json"
    assert baseline_file.exists()
    data = json.loads(baseline_file.read_text())
    assert data["schema_version"] == 1
    assert set(data["surfaces"]) == {
        "exec_policy", "tool_profile", "browser", "context_profile", "model_policy",
    }
    assert data["exceptions"] == []
    assert "Wrote" in result.output
    # Seeding is never silent: every surface's choice is printed.
    for surface in data["surfaces"]:
        assert surface in result.output


def test_seed_refuses_overwrite_without_force(pod):
    cli, shared = pod
    runner = CliRunner()
    assert runner.invoke(cli, ["pod-baseline", "seed"]).exit_code == 0
    again = runner.invoke(cli, ["pod-baseline", "seed"])
    assert again.exit_code != 0
    assert "--force" in again.output
    forced = runner.invoke(cli, ["pod-baseline", "seed", "--force"])
    assert forced.exit_code == 0, forced.output


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_seed_aborts_on_unreadable_existing_baseline(pod):
    # The overwrite guard must fail CLOSED: present-but-unreadable is an
    # abort, never "doesn't exist, proceed to clobber".
    cli, shared = pod
    runner = CliRunner()
    assert runner.invoke(cli, ["pod-baseline", "seed"]).exit_code == 0
    baseline_file = shared / "pod-baseline.json"
    original = baseline_file.read_text()
    baseline_file.chmod(0)
    try:
        result = runner.invoke(cli, ["pod-baseline", "seed", "--force"])
    finally:
        baseline_file.chmod(0o644)
    assert result.exit_code != 0
    assert "refusing" in result.output
    assert baseline_file.read_text() == original


def test_census_after_seed_reports_summary(pod):
    cli, shared = pod
    runner = CliRunner()
    assert runner.invoke(cli, ["pod-baseline", "seed"]).exit_code == 0
    result = runner.invoke(cli, ["pod-baseline", "census"])
    assert result.exit_code == 0, result.output
    assert "0 bot(s)" in result.output
    assert "Summary:" in result.output


def test_census_json_output_is_machine_readable(pod):
    cli, shared = pod
    runner = CliRunner()
    assert runner.invoke(cli, ["pod-baseline", "seed"]).exit_code == 0
    result = runner.invoke(cli, ["pod-baseline", "census", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {"rows": [], "counts": {}}


def test_census_rejects_invalid_baseline(pod):
    cli, shared = pod
    (shared / "pod-baseline.json").write_text(json.dumps({
        "schema_version": 1,
        "updated_at": "2026-08-15T00:00:00+00:00",
        "surfaces": {"exec_policy": "full"},  # missing the other four
        "exceptions": [],
    }))
    result = CliRunner().invoke(cli, ["pod-baseline", "census"])
    assert result.exit_code != 0
    assert "invalid" in result.output


# --- registration must not import pod_baseline (CLI-wide outage guard) -------
#
# cli.py calls every register_cli(...) helper at MODULE LOAD, so anything a
# registration body raises takes down the ENTIRE evolve-admin CLI. On the Linux
# pod, `from pod_baseline.schema import ...` at registration scope turned a
# missing package into a total operator-surface outage: `evolve-admin --help`,
# `deploy`, everything died with ModuleNotFoundError.
#
# The invariant pinned here is the PROPERTY, not the symptom: importing
# evolve_admin.cli must leave pod_baseline absent from sys.modules. A test that
# merely asserted `--help` works would pass again the moment someone reintroduced
# a registration-time import of some other optional package.
#
# It runs in a SUBPROCESS on purpose. A full-suite run imports pod_baseline via
# the analyzer's own tests, so an in-process sys.modules check would be vacuous.

_IMPORT_PROBE = """
import json, sys

import evolve_admin.cli as cli

json.dump({
    "pod_baseline_imported": "pod_baseline" in sys.modules,
    "pod_baseline_submodules": sorted(
        m for m in sys.modules if m.startswith("pod_baseline.")
    ),
    "group_registered": "pod-baseline" in cli.main.commands,
}, sys.stdout)
"""


def test_importing_cli_does_not_import_pod_baseline():
    """Importing the CLI must not drag in pod_baseline (packages/analyzer)."""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE],
        capture_output=True, text=True,
    )
    # Not a skip: if evolve_admin.cli cannot be imported at all, that IS the
    # outage this test guards. Surface stderr so the failure is legible.
    assert proc.returncode == 0, (
        f"importing evolve_admin.cli failed (rc={proc.returncode}):\n{proc.stderr}"
    )
    probe = json.loads(proc.stdout)

    assert not probe["pod_baseline_imported"], (
        "importing evolve_admin.cli pulled in pod_baseline "
        f"(submodules: {probe['pod_baseline_submodules']}). Registration runs at "
        "cli.py module load — a missing package there takes down the WHOLE CLI. "
        "Move the import inside the command callback that needs it."
    )
    # The other half of the invariant: keep registration cheap, not absent.
    # Deleting the registration (or wrapping it in try/except) would also make
    # the assertion above pass, while silently dropping the commands.
    assert probe["group_registered"], (
        "pod-baseline group is no longer registered on the CLI"
    )
