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


_ALL_SURFACES = [
    "exec_policy", "tool_profile", "browser", "context_profile", "model_policy",
]


def _write_bot(home, *, exec_policy, tool_profile=None):
    oc = home / ".openclaw"
    oc.mkdir(parents=True)
    cfg = {"tools": {"exec": {"security": exec_policy}}, "agents": {}}
    if tool_profile:
        cfg["tools"]["profile"] = tool_profile
    (oc / "openclaw.json").write_text(json.dumps(cfg))
    # A non-empty `rungs` array is the Custom shape → model_policy=custom.
    (oc / "evolve-tiers.json").write_text(json.dumps({"rungs": [{"slug": "x"}]}))


@pytest.fixture()
def pod_with_bots(tmp_path, monkeypatch):
    """A 3-bot pod shaped like the live fleet: one hardened bot, and four
    surfaces whose modal reading is a no-intent sentinel.

    Bot homes are redirected under tmp_path — the census resolves them
    through ``evolve_config.bot_home`` (pwd → platform home root), which
    would otherwise reach for real accounts on the test host.
    """
    import pod_baseline.census as census_mod

    shared = tmp_path / "shared"
    shared.mkdir()
    bots = {}
    for bot_id, exec_policy, profile in (
        ("bot-a", "full", None),
        ("bot-b", "full", None),
        ("bot-c", "allowlist", "coding"),
    ):
        _write_bot(tmp_path / "homes" / bot_id,
                   exec_policy=exec_policy, tool_profile=profile)
        bots[bot_id] = {"user": bot_id}
    monkeypatch.setattr(
        census_mod, "bot_home",
        lambda bot_id, network=None: tmp_path / "homes" / bot_id,
    )
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({
        "bots": bots,
        "members": sorted(bots),
        "sharedDir": str(shared),
    }))
    return _make_cli(network_path), shared


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
    # An empty pod has declared nothing, so every surface seeds undeclared.
    cli, shared = pod
    result = CliRunner().invoke(cli, ["pod-baseline", "seed"])
    assert result.exit_code == 0, result.output
    baseline_file = shared / "pod-baseline.json"
    assert baseline_file.exists()
    data = json.loads(baseline_file.read_text())
    assert data["schema_version"] == 1
    assert data["surfaces"] == {}
    assert data["undeclared"] == _ALL_SURFACES
    assert data["exceptions"] == []
    assert "Wrote" in result.output
    assert "left undeclared" in result.output
    # Seeding is never silent: every surface's choice is printed.
    for surface in _ALL_SURFACES:
        assert surface in result.output


def test_seed_declares_real_values_and_refuses_sentinels(pod_with_bots):
    # Q7(b) end to end: exec_policy carries a real modal value and gets
    # declared; the four sentinel-modal surfaces are left for the operator.
    cli, shared = pod_with_bots
    result = CliRunner().invoke(cli, ["pod-baseline", "seed"])
    assert result.exit_code == 0, result.output
    data = json.loads((shared / "pod-baseline.json").read_text())
    assert data["surfaces"] == {"exec_policy": "full"}
    assert data["undeclared"] == [
        "tool_profile", "browser", "context_profile", "model_policy",
    ]
    # The printed explanation says WHY, naming the sentinel.
    assert "(undeclared)" in result.output
    assert "no declared intent" in result.output


def test_census_splits_drift_by_direction_and_collapses_undeclared(pod_with_bots):
    cli, shared = pod_with_bots
    runner = CliRunner()
    assert runner.invoke(cli, ["pod-baseline", "seed"]).exit_code == 0
    result = runner.invoke(cli, ["pod-baseline", "census"])
    assert result.exit_code == 0, result.output

    # The spec's worked arithmetic, at fixture scale: 3 bots × 5 surfaces
    # = 15 rows → 2 conform + 1 tightened + 12 undeclared.
    assert ("Summary: 2 conform, 0 exception, 1 tightened, 0 loosened, "
            "0 divergent, 0 unreadable, 12 undeclared") in result.output
    # Hardening is informational and must not demand paperwork.
    assert "tightened" in result.output
    assert "no exception needed" in result.output
    assert "LOOSENED" not in result.output
    # Undeclared surfaces collapse to one pod-level line each carrying the
    # observed distribution — not 3 identical per-bot rows.
    assert "2 × unset, 1 × coding" in result.output
    # Exactly twice: the "Baseline:" header and the one collapsed line. Three
    # identical per-bot rows would make it five.
    assert result.output.count("tool_profile") == 2
    assert "Declare one" in result.output


def test_census_reports_a_loosened_row_as_the_fault_state(pod_with_bots):
    cli, shared = pod_with_bots
    runner = CliRunner()
    assert runner.invoke(cli, ["pod-baseline", "seed"]).exit_code == 0
    # Tighten the declared baseline under the fleet: every bot at `full` is
    # now looser than policy.
    path = shared / "pod-baseline.json"
    data = json.loads(path.read_text())
    data["surfaces"]["exec_policy"] = "deny"
    data["undeclared"] = [s for s in data["undeclared"] if s != "exec_policy"]
    path.write_text(json.dumps(data))
    result = runner.invoke(cli, ["pod-baseline", "census"])
    assert result.exit_code == 0, result.output
    assert "LOOSENED" in result.output
    assert "0 tightened" in result.output
    assert "3 loosened" in result.output


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
    assert payload == {
        "rows": [],
        # Every state pre-filled at zero — B2 reads this, and an absent key
        # would be indistinguishable from a producer that doesn't report it.
        "counts": {
            "conform": 0, "exception": 0, "tightened": 0, "loosened": 0,
            "divergent": 0, "unreadable": 0, "undeclared": 0,
        },
        "undeclared_surfaces": _ALL_SURFACES,
        "undeclared_distribution": {s: {} for s in _ALL_SURFACES},
        "undeclared_excluded": {},
        "declared_sentinel_surfaces": [],
    }


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


def test_census_rejects_a_surface_both_declared_and_undeclared(pod):
    cli, shared = pod
    (shared / "pod-baseline.json").write_text(json.dumps({
        "schema_version": 1,
        "updated_at": "2026-08-22T00:00:00+00:00",
        "surfaces": {"exec_policy": "full"},
        "undeclared": _ALL_SURFACES,  # exec_policy is in BOTH
        "exceptions": [],
    }))
    result = CliRunner().invoke(cli, ["pod-baseline", "census"])
    assert result.exit_code != 0
    assert "both declared and undeclared" in result.output


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


# --- review fixes (2026-08-23 adversarial pass) ------------------------------

def test_census_flags_a_pre_q7_baseline_that_declares_sentinels(pod_with_bots):
    """Both live pods carry one; without this the rule lands inert.

    Seeding can no longer elect `custom`/`unset`, but an existing file still
    declares them and those rows still classify conform. The census must not
    write a baseline, so naming the condition is the whole remedy it has.
    """
    cli, shared = pod_with_bots
    (shared / "pod-baseline.json").write_text(json.dumps({
        "schema_version": 1,
        "updated_at": "2026-08-17T07:51:00+00:00",
        "surfaces": {
            "exec_policy": "full", "tool_profile": "unset", "browser": "unset",
            "context_profile": "custom", "model_policy": "custom",
        },
        "exceptions": [],
    }))
    result = CliRunner().invoke(cli, ["pod-baseline", "census"])
    assert result.exit_code == 0, result.output
    assert "no-intent sentinel" in result.output
    assert "tool_profile=unset" in result.output
    assert "model_policy=custom" in result.output
    assert "seed --force" in result.output
    # The rows on the flagged surfaces really do still read conform — that IS
    # the problem being named. browser=unset and model_policy=custom match
    # every bot, so 6 of the 15 rows conform against a value nobody chose.
    conform_rows = [
        r for r in json.loads(
            CliRunner().invoke(cli, ["pod-baseline", "census", "--json"]).output
        )["rows"]
        if r["state"] == "conform" and r["surface"] in ("browser", "model_policy")
    ]
    assert len(conform_rows) == 6


def test_census_does_not_cry_sentinel_on_a_post_q7_baseline(pod_with_bots):
    cli, shared = pod_with_bots
    runner = CliRunner()
    assert runner.invoke(cli, ["pod-baseline", "seed"]).exit_code == 0
    result = runner.invoke(cli, ["pod-baseline", "census"])
    assert result.exit_code == 0, result.output
    assert "no-intent sentinel" not in result.output


def test_undeclared_line_names_the_rows_it_does_not_cover(pod_with_bots):
    # A bot whose config cannot be read must not vanish under a header that
    # says "the counts are what each surface reads today".
    cli, shared = pod_with_bots
    runner = CliRunner()
    assert runner.invoke(cli, ["pod-baseline", "seed"]).exit_code == 0
    data = json.loads((shared / "pod-baseline.json").read_text())
    data["exceptions"] = [{
        # matches bot-a's live reading, so the row classifies `exception`
        "bot_id": "bot-a", "surface": "browser", "value": "unset",
        "reason": "kiosk", "declared_at": "2026-08-22T00:00:00Z",
    }]
    (shared / "pod-baseline.json").write_text(json.dumps(data))
    result = runner.invoke(cli, ["pod-baseline", "census"])
    assert result.exit_code == 0, result.output
    # bot-a is under an exception, so it is NOT in the browser distribution —
    # and the line says so rather than implying only 2 bots exist.
    assert "2 × unset; 1 exception (listed per bot)" in result.output


def test_tightened_under_an_exception_does_not_say_no_exception_needed(pod_with_bots):
    # "no exception needed" is advice about the POD baseline. On a row whose
    # expected value already IS an exception it reads as "delete the
    # exception you deliberately declared".
    cli, shared = pod_with_bots
    runner = CliRunner()
    assert runner.invoke(cli, ["pod-baseline", "seed"]).exit_code == 0
    data = json.loads((shared / "pod-baseline.json").read_text())
    data["exceptions"] = [{
        "bot_id": "bot-c", "surface": "exec_policy", "value": "full",
        "reason": "needs host exec", "declared_at": "2026-08-22T00:00:00Z",
    }]
    (shared / "pod-baseline.json").write_text(json.dumps(data))
    result = runner.invoke(cli, ["pod-baseline", "census"])
    assert result.exit_code == 0, result.output
    # bot-c reads allowlist against an exception of full -> tightened.
    assert "tightened" in result.output
    assert "no exception needed" not in result.output


def test_census_legend_names_every_state(pod):
    """The CLI's state_mark table must not silently fall through.

    It has a `.get(state, state)` fallback, so a state missing from the
    legend renders as a bare identifier instead of a marker — invisible in
    review. Derive the roster from the module, not from a second hand-list.
    """
    import re

    import pod_baseline.schema as schema_mod
    from pod_baseline.schema import STATE_DISPLAY_ORDER

    assert set(STATE_DISPLAY_ORDER) == {
        value for name, value in vars(schema_mod).items()
        if name.startswith("STATE_") and isinstance(value, str)
    }
    source = (
        __import__("evolve_admin.pod_baseline_cli", fromlist=["x"]).__file__
    )
    body = open(source).read()
    marked = set(re.findall(r"STATE_(\w+): \"", body))
    assert {f"STATE_{m}" for m in marked} == {
        f"STATE_{state.upper()}" for state in STATE_DISPLAY_ORDER
    }


def test_divergent_says_when_the_surface_has_no_safety_ordering(pod_with_bots):
    # A divergent row means two different things: "these two values are not
    # comparable" on a surface WITH a ladder, versus "no safe direction
    # exists at all" on one without. The operator needs the difference —
    # only the second can never become tightened.
    cli, shared = pod_with_bots
    (shared / "pod-baseline.json").write_text(json.dumps({
        "schema_version": 1,
        "updated_at": "2026-08-22T00:00:00+00:00",
        "surfaces": {"model_policy": "pod-defaults", "exec_policy": "full"},
        "undeclared": ["tool_profile", "browser", "context_profile"],
        "exceptions": [],
    }))
    result = CliRunner().invoke(cli, ["pod-baseline", "census"])
    assert result.exit_code == 0, result.output
    # model_policy has no chains at all -> the explanation fires.
    assert "no safety ordering on this surface" in result.output
    assert "3 divergent" in result.output
