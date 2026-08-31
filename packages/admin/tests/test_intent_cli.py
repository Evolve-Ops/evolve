"""Tests for the `evolve-admin intent` CLI group.

Phase 2 of internal/spec-config-intent-system-2026-05-21.md ships this command
as the backfill shim for legacy deliberate-deviation values (the 6
auth_drift_filler proposals from the 2026-05-24 triage). Covers:

  - intent set writes the sidecar
  - JSON value parsing (scalars, strings, booleans)
  - --depends-on-plugin builds the right depends_on record
  - intent list surfaces what was set
  - intent revoke removes the record + returns non-zero on miss
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from evolve_admin.cli import main


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    sd = tmp_path / "shared"
    sd.mkdir()
    return sd


@pytest.fixture
def network_path(shared_dir: Path) -> Path:
    p = shared_dir / "network.json"
    p.write_text(json.dumps({
        "networkId": "test", "sharedDir": str(shared_dir),
        "bots": {"team_bot_a": {"role": "member", "user": "team_bot_a"}},
    }))
    return p


def _run(network_path: Path, args: list[str]) -> "object":
    """Invoke the CLI with the same --network override every test needs."""
    runner = CliRunner()
    return runner.invoke(main, ["--network", str(network_path), *args])


def test_intent_set_writes_sidecar(shared_dir: Path, network_path: Path):
    result = _run(network_path, [
        "intent", "set", "team_bot_a", "tools.exec.security", '"full"',
        "--reason", "codex plugin requires exec",
        "--set-by", "plugin_side_effect:codex",
        "--depends-on-plugin", "codex",
        "--shared-dir", str(shared_dir),
    ])
    assert result.exit_code == 0, result.output

    sidecar = json.loads((shared_dir / "config_intents" / "team_bot_a.json").read_text())
    assert len(sidecar["intents"]) == 1
    entry = sidecar["intents"][0]
    assert entry["field_path"] == "tools.exec.security"
    assert entry["value"] == "full"  # JSON-parsed: '"full"' → "full"
    assert entry["set_by"] == "plugin_side_effect:codex"
    assert entry["depends_on"] == {"plugin": "codex"}


def test_intent_set_parses_booleans_and_numbers(shared_dir: Path,
                                                  network_path: Path):
    result = _run(network_path, [
        "intent", "set", "team_bot_a", "tools.fs.workspaceOnly", "false",
        "--reason", "shared media folder needs read access",
        "--shared-dir", str(shared_dir),
    ])
    assert result.exit_code == 0, result.output
    sidecar = json.loads((shared_dir / "config_intents" / "team_bot_a.json").read_text())
    assert sidecar["intents"][0]["value"] is False


def test_intent_set_falls_back_to_raw_string_when_not_json(
        shared_dir: Path, network_path: Path):
    """Bare 'full' without quotes is not valid JSON; the shim must accept
    it as the string ``"full"`` rather than erroring at the operator's
    terminal."""
    result = _run(network_path, [
        "intent", "set", "team_bot_a", "tools.exec.security", "full",
        "--reason", "shorthand entry",
        "--shared-dir", str(shared_dir),
    ])
    assert result.exit_code == 0, result.output
    sidecar = json.loads((shared_dir / "config_intents" / "team_bot_a.json").read_text())
    assert sidecar["intents"][0]["value"] == "full"


def test_intent_list_shows_recorded_intents(shared_dir: Path,
                                              network_path: Path):
    _run(network_path, [
        "intent", "set", "team_bot_a", "tools.exec.security", '"full"',
        "--reason", "r1", "--shared-dir", str(shared_dir),
    ])
    _run(network_path, [
        "intent", "set", "team_bot_a", "tools.fs.workspaceOnly", "false",
        "--reason", "r2", "--shared-dir", str(shared_dir),
    ])
    result = _run(network_path, [
        "intent", "list", "team_bot_a", "--shared-dir", str(shared_dir),
    ])
    assert result.exit_code == 0, result.output
    # Rich Table can wrap or truncate cell text under narrow CliRunner-mode
    # terminals; assert on the operator-visible title + the data shape on
    # disk rather than on a specific rendering of either field path.
    assert "Intents — team_bot_a" in result.output
    sidecar = json.loads((shared_dir / "config_intents" / "team_bot_a.json").read_text())
    assert {e["field_path"] for e in sidecar["intents"]} == {
        "tools.exec.security", "tools.fs.workspaceOnly",
    }


def test_intent_list_handles_empty(shared_dir: Path, network_path: Path):
    result = _run(network_path, [
        "intent", "list", "team_bot_a", "--shared-dir", str(shared_dir),
    ])
    assert result.exit_code == 0, result.output
    assert "no intents" in result.output.lower()


def test_intent_revoke_removes_record(shared_dir: Path, network_path: Path):
    set_result = _run(network_path, [
        "intent", "set", "team_bot_a", "tools.exec.security", '"full"',
        "--reason", "r", "--shared-dir", str(shared_dir),
    ])
    assert set_result.exit_code == 0
    sidecar = json.loads((shared_dir / "config_intents" / "team_bot_a.json").read_text())
    intent_id = sidecar["intents"][0]["id"]

    revoke_result = _run(network_path, [
        "intent", "revoke", "team_bot_a", intent_id,
        "--shared-dir", str(shared_dir),
    ])
    assert revoke_result.exit_code == 0, revoke_result.output

    sidecar = json.loads((shared_dir / "config_intents" / "team_bot_a.json").read_text())
    assert sidecar["intents"] == []
    assert len(sidecar["intents_archive"]) == 1


def test_intent_revoke_unknown_exits_nonzero(shared_dir: Path,
                                              network_path: Path):
    result = _run(network_path, [
        "intent", "revoke", "team_bot_a", "intent-nonexistent",
        "--shared-dir", str(shared_dir),
    ])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()
