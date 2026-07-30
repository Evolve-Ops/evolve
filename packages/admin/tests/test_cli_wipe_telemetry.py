"""tests/test_cli_wipe_telemetry.py — wipe-telemetry CLI (Phase 1.5b §14.3).

Tests the click command that wraps ``evolve_admin.evo.tool_gaps.wipe_tool_gaps``:
  - default category is 'tool_gaps' (only choice today)
  - prompt-and-confirm path (refused → no delete)
  - --yes flag skips prompt
  - idempotent on a clean pod (nothing to delete; no error)
  - actual file deletion on a pod with records
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
_ANALYZER_PKG = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_PKG) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_PKG))

from evolve_admin.cli import main  # noqa: E402
from evolve_admin.evo import tool_gaps  # noqa: E402


def _setup_pod(tmp_path: Path) -> Path:
    """Write a minimal network.json that points sharedDir at tmp_path."""
    np = tmp_path / "network.json"
    np.write_text(json.dumps({
        "sharedDir": str(tmp_path),
        "members": [],
        "bots": {},
    }))
    return np


# ─────────────────────────────────────────────────────────────────────────────
# Help text + argument parsing
# ─────────────────────────────────────────────────────────────────────────────


def test_help_lists_tool_gaps_category():
    runner = CliRunner()
    result = runner.invoke(main, ["wipe-telemetry", "--help"])
    assert result.exit_code == 0
    assert "tool_gaps" in result.output


def test_unknown_category_rejected():
    runner = CliRunner()
    result = runner.invoke(
        main, ["wipe-telemetry", "--category", "nope", "--yes"],
    )
    assert result.exit_code != 0
    # click's error message for invalid Choice
    assert "tool_gaps" in result.output or "Invalid" in result.output


# ─────────────────────────────────────────────────────────────────────────────
# No-op on clean pod
# ─────────────────────────────────────────────────────────────────────────────


def test_noop_when_file_absent(tmp_path):
    np = _setup_pod(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main, ["--network", str(np), "wipe-telemetry", "--yes"],
    )
    assert result.exit_code == 0
    assert "No telemetry to wipe" in result.output


# ─────────────────────────────────────────────────────────────────────────────
# Yes flag — bypass confirm + delete
# ─────────────────────────────────────────────────────────────────────────────


def test_yes_flag_deletes_file(tmp_path):
    np = _setup_pod(tmp_path)
    # Seed some records via the real writer
    for i in range(3):
        tool_gaps.record_tool_gap(
            tmp_path, kind="k", scope="single_bot", fingerprint=f"fp-{i}",
        )
    target = tmp_path / "observations" / "tool_gaps.jsonl"
    assert target.exists()

    runner = CliRunner()
    result = runner.invoke(
        main, ["--network", str(np), "wipe-telemetry", "--yes"],
    )
    assert result.exit_code == 0
    assert not target.exists()
    assert "Wiped" in result.output


# ─────────────────────────────────────────────────────────────────────────────
# Confirm prompt path
# ─────────────────────────────────────────────────────────────────────────────


def test_prompt_refused_no_delete(tmp_path):
    np = _setup_pod(tmp_path)
    tool_gaps.record_tool_gap(
        tmp_path, kind="k", scope="single_bot", fingerprint="fp",
    )
    target = tmp_path / "observations" / "tool_gaps.jsonl"
    assert target.exists()

    runner = CliRunner()
    result = runner.invoke(
        main, ["--network", str(np), "wipe-telemetry"],
        input="n\n",
    )
    assert result.exit_code == 0
    # File still there — operator said no
    assert target.exists()
    assert "Cancelled" in result.output


def test_prompt_confirmed_deletes(tmp_path):
    np = _setup_pod(tmp_path)
    tool_gaps.record_tool_gap(
        tmp_path, kind="k", scope="single_bot", fingerprint="fp",
    )
    target = tmp_path / "observations" / "tool_gaps.jsonl"

    runner = CliRunner()
    result = runner.invoke(
        main, ["--network", str(np), "wipe-telemetry"],
        input="y\n",
    )
    assert result.exit_code == 0
    assert not target.exists()
    assert "Wiped" in result.output


def test_prompt_surfaces_file_size(tmp_path):
    """The prompt shows the byte count so the operator knows what they're
    deleting. Useful when they expect 'a few records' but the file is
    huge (signal of unexpected activity)."""
    np = _setup_pod(tmp_path)
    for i in range(5):
        tool_gaps.record_tool_gap(
            tmp_path, kind="k", scope="single_bot", fingerprint=f"fp-{i}",
        )

    runner = CliRunner()
    result = runner.invoke(
        main, ["--network", str(np), "wipe-telemetry"],
        input="n\n",
    )
    assert result.exit_code == 0
    # Output mentions "bytes" so the operator sees the size
    assert "bytes" in result.output


# ─────────────────────────────────────────────────────────────────────────────
# Network.json read failure
# ─────────────────────────────────────────────────────────────────────────────


def test_unreadable_network_returns_nonzero(tmp_path):
    """If network.json is malformed, the command should exit non-zero
    rather than silently fail — operator needs to know."""
    np = tmp_path / "network.json"
    np.write_text("{ not valid json")
    runner = CliRunner()
    result = runner.invoke(
        main, ["--network", str(np), "wipe-telemetry", "--yes"],
    )
    assert result.exit_code != 0


# ─────────────────────────────────────────────────────────────────────────────
# Wipe doesn't touch other files in observations/
# ─────────────────────────────────────────────────────────────────────────────


def test_wipe_only_removes_tool_gaps_file(tmp_path):
    """Per spec §14.3, the wipe path is scoped to telemetry. Other files
    under observations/ (e.g. per-bot inference snapshots) must not be
    touched."""
    np = _setup_pod(tmp_path)
    tool_gaps.record_tool_gap(
        tmp_path, kind="k", scope="single_bot", fingerprint="fp",
    )
    # Drop a sibling file that mimics an unrelated observations artifact.
    obs_dir = tmp_path / "observations"
    sibling = obs_dir / "unrelated.jsonl"
    sibling.write_text("don't touch me\n")

    runner = CliRunner()
    result = runner.invoke(
        main, ["--network", str(np), "wipe-telemetry", "--yes"],
    )
    assert result.exit_code == 0
    assert not (obs_dir / "tool_gaps.jsonl").exists()
    # Sibling preserved
    assert sibling.exists()
    assert sibling.read_text() == "don't touch me\n"
