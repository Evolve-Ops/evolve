"""tools/test_required_check_quarantine_ratchet.py — the no-growth cap on
tools/required-check-quarantine.txt (D-CS9), same shape as
tools/quarantine-ratchet for ci-quarantine.txt.
"""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

_TOOLS = Path(__file__).resolve().parent


def _load(name: str, filename: str):
    """Load an extensionless tools/ executable by path."""
    loader = SourceFileLoader(name, str(_TOOLS / filename))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _write(path: Path, n_lines: int) -> None:
    path.write_text("".join(f"line {i}\n" for i in range(n_lines)), encoding="utf-8")


def test_required_check_ratchet_refuses_growth(tmp_path, monkeypatch):
    ratchet = _load("required_check_quarantine_ratchet", "required-check-quarantine-ratchet")
    quarantine = tmp_path / "required-check-quarantine.txt"
    baseline = tmp_path / "required-check-quarantine-baseline.txt"
    monkeypatch.setattr(ratchet, "QUARANTINE_PATH", quarantine)
    monkeypatch.setattr(ratchet, "BASELINE_PATH", baseline)
    monkeypatch.setattr("sys.argv", ["required-check-quarantine-ratchet"])

    _write(quarantine, 10)
    baseline.write_text("10\n", encoding="utf-8")
    assert ratchet.main() == 0  # at baseline

    _write(quarantine, 12)
    assert ratchet.main() == 1  # grew past baseline — refused

    _write(quarantine, 8)
    assert ratchet.main() == 0  # shrinking is always fine


def test_ratchet_update_baseline_freezes_the_current_count(tmp_path, monkeypatch):
    ratchet = _load("required_check_quarantine_ratchet_2", "required-check-quarantine-ratchet")
    quarantine = tmp_path / "required-check-quarantine.txt"
    baseline = tmp_path / "required-check-quarantine-baseline.txt"
    monkeypatch.setattr(ratchet, "QUARANTINE_PATH", quarantine)
    monkeypatch.setattr(ratchet, "BASELINE_PATH", baseline)
    monkeypatch.setattr("sys.argv", ["required-check-quarantine-ratchet", "--update-baseline"])

    _write(quarantine, 5)
    assert ratchet.main() == 0
    assert ratchet._load_baseline() == 5

    monkeypatch.setattr("sys.argv", ["required-check-quarantine-ratchet"])
    _write(quarantine, 6)
    assert ratchet.main() == 1  # baseline is now 5 — one line of growth is refused
