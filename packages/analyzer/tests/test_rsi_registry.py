"""tests/test_rsi_registry.py — registry charter loading + fingerprint + records."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from registry import (  # noqa: E402
    CharterFingerprintMismatch,
    GeneratorNotFound,
    Registry,
    compute_charter_fingerprint,
)


_VALID_CHARTER_YAML = """\
id: test_generator
type: guardian
dimension: substrate_health
purpose: Test generator used by registry tests
cadence: hourly
invariants:
  - id: action_kinds
    description: only investigation allowed
    check_kind: action_kind_allowed
    params:
      allowlist: [Investigation]
"""


def _setup_dir(tmp_path, yaml_content=_VALID_CHARTER_YAML, gen_id="test_generator"):
    code_dir = tmp_path / "generators"
    records_dir = tmp_path / "records"
    gen_dir = code_dir / gen_id
    gen_dir.mkdir(parents=True)
    (gen_dir / "charter.yaml").write_text(yaml_content)
    return code_dir, records_dir


def test_empty_generators_dir_loads_nothing(tmp_path):
    reg = Registry(
        generators_code_dir=tmp_path / "generators",
        records_dir=tmp_path / "records",
    )
    loaded = reg.load_all()
    assert loaded == {}
    assert reg.load_errors() == {}


def test_first_load_creates_record(tmp_path):
    code_dir, records_dir = _setup_dir(tmp_path)
    reg = Registry(generators_code_dir=code_dir, records_dir=records_dir)
    loaded = reg.load_all()

    assert "test_generator" in loaded
    tg = loaded["test_generator"]
    assert tg.charter.type == "guardian"
    assert tg.record.charter_fingerprint == compute_charter_fingerprint(
        _VALID_CHARTER_YAML
    )
    assert tg.record.budget_policy == "duty"  # guardian → duty
    assert (records_dir / "test_generator.json").exists()


def test_second_load_reuses_record(tmp_path):
    code_dir, records_dir = _setup_dir(tmp_path)
    reg1 = Registry(generators_code_dir=code_dir, records_dir=records_dir)
    reg1.load_all()

    reg2 = Registry(generators_code_dir=code_dir, records_dir=records_dir)
    loaded = reg2.load_all()
    assert loaded["test_generator"].record.charter_fingerprint == compute_charter_fingerprint(
        _VALID_CHARTER_YAML
    )


def test_fingerprint_mismatch_raises_in_strict_mode(tmp_path):
    code_dir, records_dir = _setup_dir(tmp_path)
    reg = Registry(generators_code_dir=code_dir, records_dir=records_dir)
    reg.load_all()  # first load creates record

    # Modify the charter
    charter_path = code_dir / "test_generator" / "charter.yaml"
    altered = charter_path.read_text().replace(
        "Test generator", "Test generator (altered)"
    )
    charter_path.write_text(altered)

    reg2 = Registry(generators_code_dir=code_dir, records_dir=records_dir)
    with pytest.raises(CharterFingerprintMismatch):
        reg2.load_all(strict=True)


def test_fingerprint_mismatch_collected_in_lenient_mode(tmp_path):
    code_dir, records_dir = _setup_dir(tmp_path)
    reg = Registry(generators_code_dir=code_dir, records_dir=records_dir)
    reg.load_all()

    charter_path = code_dir / "test_generator" / "charter.yaml"
    charter_path.write_text(
        charter_path.read_text().replace("Test", "Altered")
    )

    reg2 = Registry(generators_code_dir=code_dir, records_dir=records_dir)
    reg2.load_all(strict=False)
    assert "test_generator" in reg2.load_errors()
    assert "test_generator" not in reg2.all_loaded()


def test_get_raises_for_unloaded(tmp_path):
    reg = Registry(
        generators_code_dir=tmp_path / "generators",
        records_dir=tmp_path / "records",
    )
    reg.load_all()
    with pytest.raises(GeneratorNotFound):
        reg.get("nope")


def test_update_status_and_persist(tmp_path):
    code_dir, records_dir = _setup_dir(tmp_path)
    reg = Registry(generators_code_dir=code_dir, records_dir=records_dir)
    reg.load_all()

    reg.update_status("test_generator", "quarantined", reason="broken")
    assert reg.get("test_generator").record.status == "quarantined"

    # Reload — change persisted
    reg2 = Registry(generators_code_dir=code_dir, records_dir=records_dir)
    reg2.load_all()
    assert reg2.get("test_generator").record.status == "quarantined"
    assert reg2.get("test_generator").record.quarantine_reason == "broken"


def test_update_track_record(tmp_path):
    code_dir, records_dir = _setup_dir(tmp_path)
    reg = Registry(generators_code_dir=code_dir, records_dir=records_dir)
    reg.load_all()

    def bump(tr):
        tr.proposals_emitted += 1
        tr.proposals_verified_success += 1

    reg.update_track_record("test_generator", bump)
    updated = reg.get("test_generator").record.track_record
    assert updated.proposals_emitted == 1
    assert updated.proposals_verified_success == 1


def test_active_generators_filters_paused(tmp_path):
    code_dir, records_dir = _setup_dir(tmp_path)
    reg = Registry(generators_code_dir=code_dir, records_dir=records_dir)
    reg.load_all()

    assert "test_generator" in reg.active_generators()
    reg.update_status("test_generator", "paused")
    assert "test_generator" not in reg.active_generators()


def test_budget_policy_matches_generator_type(tmp_path):
    for gen_type, expected in [
        ("optimizer", "competitive"),
        ("guardian", "duty"),
        ("meta_guardian", "meta"),
    ]:
        # Replace both the type AND the id so the gen_id inside the yaml
        # matches the directory name.
        yaml = (
            _VALID_CHARTER_YAML
            .replace("type: guardian", f"type: {gen_type}")
            .replace("id: test_generator", f"id: gen_{gen_type}")
        )
        code_dir, records_dir = _setup_dir(
            tmp_path / f"case_{gen_type}",
            yaml_content=yaml,
            gen_id=f"gen_{gen_type}",
        )
        reg = Registry(generators_code_dir=code_dir, records_dir=records_dir)
        reg.load_all()
        rec = reg.get(f"gen_{gen_type}").record
        assert rec.budget_policy == expected
