"""tests/test_rsi_dry_run_tool.py — pin the rsi_dry_run CLI's contract.

The tool's value is operator-facing: someone tuning an AGENTS.md or
designing an exclusion section runs it and sees what the substrate
would emit. These tests pin the invocation contract + the conditional
substrate-availability report so a future change to the tool's
import-detection doesn't silently degrade what operators see.

Subprocess-based — exercises the actual CLI entry point.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


_ANALYZER_DIR = Path(__file__).parent.parent
_SAMPLES = _ANALYZER_DIR / "tools" / "samples"


def _run_cli(args: list[str]) -> subprocess.CompletedProcess:
    """Run the CLI as a subprocess so we exercise the actual
    argparse + module-loading path the operator hits. Anchor cwd at
    the analyzer dir so the `tools` package + `samples` directory
    resolve cleanly."""
    return subprocess.run(
        [sys.executable, "-m", "tools.rsi_dry_run", *args],
        cwd=_ANALYZER_DIR,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_cli_exits_2_without_agents_md_arg():
    """The CLI's --agents-md is required. Operators get a clean
    argparse error rather than a confusing traceback."""
    result = _run_cli(["--pattern", "workout:tracking:8:8"])
    assert result.returncode == 2
    assert "agents-md" in result.stderr


def test_cli_exits_2_when_agents_md_missing():
    """Bad path → operator-readable error message, not a Python
    traceback. The whole point of the tool is to remove friction."""
    result = _run_cli([
        "--agents-md", "/nope/does-not-exist.md",
        "--pattern", "workout:tracking:8:8",
    ])
    assert result.returncode == 2
    assert "not found" in result.stderr.lower()


def test_cli_exits_2_when_no_pattern_given():
    """Empty pattern list → nothing to exercise, exit 2 with hint."""
    result = _run_cli(["--agents-md", str(_SAMPLES / "fitness-coach.md")])
    assert result.returncode == 2
    assert "pattern" in result.stderr.lower()


def test_cli_rejects_malformed_pattern():
    """Pattern shape is enforced. A 2-piece pattern (missing sessions
    + days) is a structural error, not a runtime one."""
    result = _run_cli([
        "--agents-md", str(_SAMPLES / "fitness-coach.md"),
        "--pattern", "workout:tracking",  # too few pieces
    ])
    assert result.returncode == 2


def test_cli_runs_to_completion_on_fitness_sample():
    """Happy path — the tool exercises every available producer +
    consumer and exits 0. The fitness sample + workout pattern is the
    canonical worked example."""
    result = _run_cli([
        "--agents-md", str(_SAMPLES / "fitness-coach.md"),
        "--pattern", "workout:tracking:8:8",
    ])
    assert result.returncode == 0, (
        f"CLI exited non-zero: stderr={result.stderr!r}"
    )
    # Substrate-availability report present.
    assert "Phase 2 substrate availability" in result.stdout
    # cap-gap monitor (on main) must be marked available.
    assert "✓ capability_gap_monitor" in result.stdout


def test_cli_substrate_status_includes_every_known_module():
    """The status report must list every module the tool can probe
    for — operators read this list to know what's wired up vs. open
    PR territory. Adding a new module to the substrate without
    updating this report would silently degrade the dry-run."""
    result = _run_cli([
        "--agents-md", str(_SAMPLES / "fitness-coach.md"),
        "--pattern", "workout:tracking:8:8",
    ])
    assert result.returncode == 0
    out = result.stdout
    for name in (
        "capability_gap_monitor",
        "app_suggester",
        "engagement_amplifier_monitor",
        "engagement_amplifier",
        "pod_capability_lift",
        "anti_domains",
    ):
        assert name in out, (
            f"{name} missing from substrate availability report"
        )


def test_cli_quiet_flag_suppresses_substrate_report():
    """--quiet hides the substrate + anti-domain report (operators
    scripting the tool don't always want the chatter). The detection
    output stays."""
    result = _run_cli([
        "--agents-md", str(_SAMPLES / "fitness-coach.md"),
        "--pattern", "workout:tracking:8:8",
        "--quiet",
    ])
    assert result.returncode == 0
    assert "Phase 2 substrate availability" not in result.stdout
    # Detection output must still be present.
    assert "capability_gap_monitor:" in result.stdout


def test_cli_reports_zero_detections_on_weak_pattern():
    """Below-threshold pattern (1 session, 1 day) → cap-gap monitor
    must report 0 detections. The point is to show operators why a
    real proposal didn't fire — silence here would be confusing."""
    result = _run_cli([
        "--agents-md", str(_SAMPLES / "fitness-coach.md"),
        "--pattern", "workout:tracking:1:1",
    ])
    assert result.returncode == 0
    assert "capability_gap_monitor: 0 detections" in result.stdout


def test_cli_emits_proposal_when_pattern_strong():
    """Strong pattern + matching catalog → at least one app_suggester
    proposal. End-to-end proof the producer/consumer pair is wired
    correctly."""
    result = _run_cli([
        "--agents-md", str(_SAMPLES / "fitness-coach.md"),
        "--pattern", "workout:tracking:8:8",
    ])
    assert result.returncode == 0
    assert "app_suggester proposals: 1 proposal(s)" in result.stdout


def test_sample_fixtures_present():
    """The shipped sample fixtures must exist + parse as text. They
    are the operator's quickstart — a broken fixture would block
    'just try the tool'."""
    for name in (
        "fitness-coach.md",
        "general-assistant.md",
        "sailing-bot-with-exclusions.md",
    ):
        path = _SAMPLES / name
        assert path.exists(), f"sample fixture missing: {name}"
        text = path.read_text(encoding="utf-8")
        assert text.strip(), f"sample fixture empty: {name}"


def test_sailing_sample_has_out_of_scope_section():
    """The sailing sample's value is demonstrating the anti-domain
    convention. Without an `## Out of scope` section it's just
    another sample — pin the marker exists."""
    text = (_SAMPLES / "sailing-bot-with-exclusions.md").read_text(
        encoding="utf-8"
    )
    assert "## Out of scope" in text
    assert "fitness" in text
