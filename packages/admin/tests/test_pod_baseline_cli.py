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
