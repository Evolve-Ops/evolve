"""Tests for the auto-generated output declaration contract + lint.

Spec: docs/spec-footprint-2026-06-18.md (§ "Auto-generated output declaration
contract"). Motivating audit: docs/footprint-disk-output-audit-2026-06-28.md.

The registry (`footprint.components`) declares, per output, a retention policy +
a consumer for every auto-generated surface write; `tools/footprint-output-lint`
enforces that no new producer ships an undeclared record/log under
``{shared_dir}/**`` or ``~/.openclaw/workspace/evolve/**``. These tests pin the
proof artifacts:

  * the registry is internally consistent + no stale (deleted-file) declarations;
  * the forbidden shape (consumer: none + no retention) FAILS;
  * ALL current producers PASS (registry backfilled; --all --strict exit 0);
  * a NEW undeclared ``.jsonl`` record write → lint BLOCKS;
  * a bounded state overwrite + a /tmp write → NOT flagged (scope guard).
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

from footprint import components as reg
from footprint.components import (
    Consumer,
    OutputDeclaration,
    Retention,
    SURFACE_SHARED,
    named,
    no_consumer,
)

_TOOL = Path(__file__).resolve().parents[3] / "tools" / "footprint-output-lint"


def _load_tool():
    loader = importlib.machinery.SourceFileLoader("footprint_output_lint", str(_TOOL))
    spec = importlib.util.spec_from_loader("footprint_output_lint", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["footprint_output_lint"] = mod
    loader.exec_module(mod)
    return mod


lint = _load_tool()


def _od(**kw) -> OutputDeclaration:
    base = dict(
        path_glob="x/<id>.json",
        surface=SURFACE_SHARED,
        writer="packages/analyzer/signals/store.py:write_signal",
        cadence="per event",
        volume_files="1",
        volume_bytes="1KB",
        retention=Retention(pruner="cron", window="30d"),
        consumer=named("reader", "packages/analyzer/x.py:read"),
    )
    base.update(kw)
    return OutputDeclaration(**base)


# ── Registry internal consistency ────────────────────────────────────────────
def test_registry_is_internally_consistent():
    assert reg.consistency_errors() == []


def test_no_stale_declarations():
    assert lint.stale_declaration_errors() == []


def test_registry_has_substantive_backfill():
    # Sanity floor: the backfill enumerated the real producer landscape.
    assert len(reg.FOOTPRINT_COMPONENTS) >= 12
    assert len(reg.iter_outputs()) >= 20


def test_every_writer_file_exists_on_disk():
    repo_root = _TOOL.resolve().parents[1]
    missing = [
        f for f in reg.claimed_writer_files()
        if f.endswith(".py") and not (repo_root / f).is_file()
    ]
    assert missing == [], f"writer references nonexistent files: {missing}"


# ── Proof: the forbidden shapes FAIL ─────────────────────────────────────────
def test_named_consumer_with_retention_passes():
    assert reg.output_errors(_od(), where="t") == []


def test_consumer_none_with_finite_retention_passes():
    od = _od(consumer=no_consumer("forensics only"),
             retention=Retention(pruner="cron", window="14d"))
    assert reg.output_errors(od, where="t") == []


def test_consumer_none_without_retention_is_forbidden():
    # The _ingested failure mode: no reader AND no time-pruning.
    od = _od(consumer=no_consumer("forensics only"),
             retention=Retention(pruner="nobody", window="unbounded",
                                  justification="x"))
    errs = reg.output_errors(od, where="t")
    assert any("consumer: none with an unbounded retention" in e for e in errs)


def test_consumer_none_without_justification_fails():
    od = _od(consumer=Consumer(none=True), retention=Retention("cron", "30d"))
    errs = reg.output_errors(od, where="t")
    assert any("justification" in e for e in errs)


def test_unbounded_window_requires_justification():
    od = _od(retention=Retention(pruner="op", window="unbounded"))
    errs = reg.output_errors(od, where="t")
    assert any("REQUIRES a justification" in e for e in errs)


def test_named_consumer_requires_reader_and_read_site():
    od = _od(consumer=Consumer(reader="someone"))  # no read_site
    errs = reg.output_errors(od, where="t")
    assert any("reader and a read_site" in e for e in errs)


def test_writer_must_point_at_py_file():
    od = _od(writer="not_a_python_path")
    errs = reg.output_errors(od, where="t")
    assert any("must point at a .py file" in e for e in errs)


# ── Proof: all current producers pass ────────────────────────────────────────
def test_lint_all_strict_exit_zero(monkeypatch):
    monkeypatch.setattr(sys, "argv",
                        ["footprint-output-lint", "--all", "--strict"])
    assert lint.main() == 0


# ── Proof: a NEW undeclared record write is caught ───────────────────────────
def test_new_jsonl_record_write_is_blocked(tmp_path):
    fixture = tmp_path / "fresh_producer.py"
    fixture.write_text(
        "import json\n"
        "def run(shared_dir):\n"
        "    path = shared_dir / 'newthing' / 'log.jsonl'\n"
        "    with open(path, 'a') as f:\n"
        "        f.write(json.dumps({}) + '\\n')\n",
        encoding="utf-8",
    )
    high, _ambiguous = lint.scan_file(fixture)
    assert high, "a new .jsonl record write under shared_dir must be high-confidence"


def test_new_record_subdir_write_is_blocked(tmp_path):
    fixture = tmp_path / "fresh_signals.py"
    fixture.write_text(
        "def run(shared_dir, sig):\n"
        "    p = shared_dir / 'signals' / 'firing' / (sig + '.json')\n"
        "    p.write_text('{}')\n",
        encoding="utf-8",
    )
    high, _ = lint.scan_file(fixture)
    assert high


# ── Proof: bounded state + non-surface writes are NOT flagged (scope guard) ──
def test_bounded_state_overwrite_not_flagged(tmp_path):
    # A single overwritten state file under shared_dir (not record-shaped) is a
    # bounded store, out of scope.
    fixture = tmp_path / "state_store.py"
    fixture.write_text(
        "def save(shared_dir, data):\n"
        "    path = shared_dir / 'breakers' / 'state.json'\n"
        "    path.write_text(data)\n",
        encoding="utf-8",
    )
    high, ambiguous = lint.scan_file(fixture)
    assert not high and not ambiguous


def test_tmp_write_not_flagged(tmp_path):
    fixture = tmp_path / "tmp_writer.py"
    fixture.write_text(
        "def stage(content):\n"
        "    with open('/tmp/scratch.jsonl', 'a') as f:\n"
        "        f.write(content)\n",
        encoding="utf-8",
    )
    high, ambiguous = lint.scan_file(fixture)
    assert not high and not ambiguous


def test_read_open_not_flagged(tmp_path):
    # Reading a record file is not a write.
    fixture = tmp_path / "reader.py"
    fixture.write_text(
        "def load(shared_dir):\n"
        "    p = shared_dir / 'signals' / 'log' / 'x.jsonl'\n"
        "    with open(p) as f:\n"
        "        return f.read()\n",
        encoding="utf-8",
    )
    high, ambiguous = lint.scan_file(fixture)
    assert not high and not ambiguous
