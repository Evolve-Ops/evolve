"""Tests for `evolve-admin migrate-generator-records`.

Charter changes invalidate `GeneratorRecord.charter_fingerprint`. Without
this command, the registry refuses to load the generator on next run and
the deployed pod stops emitting proposals from it. The migration is
operator-triggered and dry-run by default.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from evolve_admin.cli import main as cli_main  # noqa: E402
from registry.charter_loader import compute_charter_fingerprint  # noqa: E402


def _seed_record(records_dir: Path, gen_id: str, fingerprint: str) -> Path:
    records_dir.mkdir(parents=True, exist_ok=True)
    p = records_dir / f"{gen_id}.json"
    p.write_text(json.dumps({
        "id": gen_id,
        "schema_version": 1,
        "charter_fingerprint": fingerprint,
        "config": {},
        "state": {},
        "track_record": {},
        "status": "active",
        "budget_policy": "duty",
    }, indent=2), encoding="utf-8")
    return p


@pytest.fixture
def fake_network(tmp_path):
    """Minimal network.json for the click root command."""
    net = tmp_path / "network.json"
    net.write_text(json.dumps({
        "schema_version": 1,
        "members": [],
        "primary_user": "tester",
    }))
    return net


def test_dry_run_lists_mismatches_without_changing_files(tmp_path, fake_network):
    shared = tmp_path / "shared"
    record = _seed_record(shared / "generators", "security_warden", "OUTDATED" * 8)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["--network", str(fake_network),
         "migrate-generator-records",
         "--shared-dir", str(shared)],
    )
    assert result.exit_code == 0, result.output
    assert "out of date" in result.output
    assert "security_warden" in result.output
    # Dry-run did not rewrite the file
    assert json.loads(record.read_text())["charter_fingerprint"].startswith("OUTDATED")


def test_apply_rewrites_records_to_match_current_charter(tmp_path, fake_network):
    shared = tmp_path / "shared"
    _seed_record(shared / "generators", "security_warden", "OUTDATED" * 8)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["--network", str(fake_network),
         "migrate-generator-records",
         "--shared-dir", str(shared),
         "--apply"],
    )
    assert result.exit_code == 0, result.output

    # The deployed charter on the laptop is the source of truth for the
    # expected fingerprint.
    charter = (
        _ANALYZER_DIR / "generators" / "security_warden" / "charter.yaml"
    )
    expected = compute_charter_fingerprint(charter.read_text(encoding="utf-8"))

    record_path = shared / "generators" / "security_warden.json"
    assert json.loads(record_path.read_text())["charter_fingerprint"] == expected


def test_no_action_when_records_already_current(tmp_path, fake_network):
    shared = tmp_path / "shared"
    charter = (
        _ANALYZER_DIR / "generators" / "security_warden" / "charter.yaml"
    )
    fingerprint = compute_charter_fingerprint(charter.read_text(encoding="utf-8"))
    _seed_record(shared / "generators", "security_warden", fingerprint)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["--network", str(fake_network),
         "migrate-generator-records",
         "--shared-dir", str(shared)],
    )
    assert result.exit_code == 0, result.output
    assert "match their charter fingerprints" in result.output


def test_missing_records_dir_is_not_an_error(tmp_path, fake_network):
    """A fresh pod with no records dir yet shouldn't fail the migration."""
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["--network", str(fake_network),
         "migrate-generator-records",
         "--shared-dir", str(tmp_path / "no-such-shared")],
    )
    assert result.exit_code == 0, result.output
    # Rich console wraps output; collapse whitespace before substring match.
    output = " ".join(result.output.split())
    assert "nothing to migrate" in output
